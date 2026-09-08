import json

from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_cache_list_shape(monkeypatch):
    from stremiosrv import cache as cachemod
    monkeypatch.setattr(
        cachemod, "scan_cache",
        lambda root: [{"name": "a.iso", "size": 10, "mtime": 1.0}],
    )
    client = TestClient(create_app())  # engine is None
    body = client.get("/cache.json").json()
    assert body == [
        {"name": "a.iso", "size": 10, "mtime": 1.0, "active": False, "infoHash": None}
    ]


def test_cache_list_marks_active(monkeypatch):
    from stremiosrv import cache as cachemod
    monkeypatch.setattr(
        cachemod, "scan_cache",
        lambda root: [{"name": "a.iso", "size": 10, "mtime": 1.0}],
    )

    class FakeEngine:
        def name_to_hash(self):
            return {"a.iso": "deadbeef"}

    client = TestClient(create_app(engine=FakeEngine()))
    body = client.get("/cache.json").json()
    assert body[0]["active"] is True
    assert body[0]["infoHash"] == "deadbeef"


def test_cache_list_idle_item_gets_infohash_from_index(monkeypatch):
    """An item not in the live engine map gets infoHash from the persisted index, active=False."""
    from stremiosrv import cache as cachemod
    monkeypatch.setattr(
        cachemod, "scan_cache",
        lambda root: [{"name": "a.iso", "size": 10, "mtime": 1.0}],
    )
    monkeypatch.setattr(
        cachemod, "load_name_index",
        lambda root: {"a.iso": "cafebabe"},
    )
    client = TestClient(create_app())  # engine is None -> no live entry
    body = client.get("/cache.json").json()
    assert body == [
        {"name": "a.iso", "size": 10, "mtime": 1.0, "active": False, "infoHash": "cafebabe"}
    ]


def test_cache_remove_rejects_unsafe_names():
    client = TestClient(create_app())
    for bad in ["../x", "a/b", "..", ".", ""]:
        resp = client.post("/cache/remove", json={"name": bad})
        assert resp.status_code == 400, bad


def test_cache_remove_rejects_protected():
    client = TestClient(create_app())
    resp = client.post("/cache/remove", json={"name": "certificates.pem"})
    assert resp.status_code == 400


def test_cache_remove_valid(monkeypatch):
    from stremiosrv import cache as cachemod
    removed = []
    monkeypatch.setattr(cachemod, "_remove", lambda p: removed.append(p))
    client = TestClient(create_app())
    resp = client.post("/cache/remove", json={"name": "a.iso"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert removed and removed[0].endswith("a.iso")


def test_cache_remove_reclaims_the_partfile_and_resume_record(tmp_path):
    """The sibling route /library/api/remove learned this the expensive way: a torrent leaves a
    `.<infohash>.parts` holding file beside its directory, and one on a real box held 30 GB, so
    deleting only the directory reclaimed almost nothing. This route deleted only the directory."""
    from stremiosrv.config import Settings

    root = tmp_path / "cache"
    (root / ".resume").mkdir(parents=True)
    (root / "a.iso").mkdir()
    ih = "a" * 40
    part = root / f".{ih}.parts"
    part.write_bytes(b"y" * 4096)
    resume = root / ".resume" / f"{ih}.fastresume"
    resume.write_bytes(b"z" * 32)

    class FakeEngine:
        def name_to_hash(self):
            return {"a.iso": ih}

        def remove(self, _ih):
            pass

    client = TestClient(create_app(settings=Settings(cache_root=str(root)), engine=FakeEngine()))
    assert client.post("/cache/remove", json={"name": "a.iso"}).status_code == 200
    assert not (root / "a.iso").exists(), "the data directory survived"
    assert not part.exists(), "the .parts holding file survived -- most of the data lives there"
    assert not resume.exists(), "the fast-resume record survived"


def test_cache_remove_finds_the_hash_for_a_title_the_engine_never_loaded(tmp_path):
    """Only pinned and wanted torrents are re-added to the session at startup, so the engine map is
    empty for an ordinary cached title -- and without the name index this route would leave its
    partfile behind, which is the whole reason the file is worth deleting."""
    from stremiosrv.config import Settings

    root = tmp_path / "cache"
    (root / ".resume").mkdir(parents=True)
    (root / "a.iso").mkdir()
    ih = "b" * 40
    (root / ".resume" / "index.json").write_text(json.dumps({"a.iso": ih}), encoding="utf-8")
    part = root / f".{ih}.parts"
    part.write_bytes(b"y" * 4096)

    client = TestClient(create_app(settings=Settings(cache_root=str(root))))
    assert client.post("/cache/remove", json={"name": "a.iso"}).status_code == 200
    assert not part.exists(), "the partfile survived because only the session was consulted"


def test_cache_remove_stops_active_torrent(monkeypatch):
    from stremiosrv import cache as cachemod
    monkeypatch.setattr(cachemod, "_remove", lambda p: None)
    stopped = []

    class FakeEngine:
        def name_to_hash(self):
            return {"a.iso": "deadbeef"}

        def remove(self, ih):
            stopped.append(ih)

    client = TestClient(create_app(engine=FakeEngine()))
    client.post("/cache/remove", json={"name": "a.iso"})
    assert stopped == ["deadbeef"]
