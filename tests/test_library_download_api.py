import pytest
from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.config import Settings
from stremiosrv.library import api as lib
from stremiosrv.library import labels
from stremiosrv.torrent.engine import PinSpaceError

HTTPS = {"X-Forwarded-Proto": "https"}
USER = {"_id": "user-1", "email": "owner@example.com"}
MAGNET = "magnet:?xt=urn:btih:aabbccddeeff00112233445566778899aabbccdd"
IH = "aabbccddeeff00112233445566778899aabbccdd"


class FakeHandle:
    def __init__(self, ih):
        self._ih = ih

    def info_hash(self):
        return self._ih


class FakeEngine:
    def __init__(self, pin_error=None, add_error=None, names=None):
        self.added, self.pinned, self.removed, self.unpinned = [], [], [], []
        self.unwanted = []
        self.wanted = []  # the selectors each download asked for -- NOT pins
        self.pin_error = pin_error
        self.add_error = add_error
        self._names = names or {}

    def add(self, magnet, trackers=None):
        if self.add_error:
            raise self.add_error
        self.added.append(magnet)
        return FakeHandle(IH)

    def want(self, ih, spec=None):
        self.wanted.append(spec)

    def unwant(self, ih):
        self.unwanted.append(ih)

    def pin(self, ih):
        if self.pin_error:
            raise self.pin_error
        self.pinned.append(ih)
        return {"infoHash": ih}

    def unpin(self, ih):
        self.unpinned.append(ih)

    def remove(self, ih):
        self.removed.append(ih)

    def name_to_hash(self):
        return self._names

    def tracked_status(self):
        return []


def _signed_in(tmp_path, monkeypatch, engine):
    monkeypatch.setattr(lib.certcheck, "cert_san", lambda p: "DNS:stremio.example.com")
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    s = Settings(library_ui=True, cache_root=str(tmp_path))
    c = TestClient(create_app(settings=s, engine=engine), base_url="https://testserver")
    c.post("/library/api/session", json={"authKey": "good"}, headers=HTTPS)
    return c


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    eng = FakeEngine()
    return _signed_in(tmp_path, monkeypatch, eng), eng, tmp_path


def test_download_requires_a_session(tmp_path, monkeypatch):
    monkeypatch.setattr(lib.certcheck, "cert_san", lambda p: "DNS:stremio.example.com")
    s = Settings(library_ui=True, cache_root=str(tmp_path))
    c = TestClient(create_app(settings=s, engine=FakeEngine()), base_url="https://testserver")
    assert c.post("/library/api/download", json={"magnet": MAGNET},
                  headers=HTTPS).status_code == 401


def test_download_adds_and_wants_but_does_not_pin(ctx):
    """A download is the same operation as playback: the file is wanted, nothing more. Pinning it
    handed it an eviction exemption nobody asked for, which is how a season pack came to sit far
    above the cache budget with the evictor unable to reclaim it. Keeping is a manual act."""
    c, eng, _ = ctx
    r = c.post("/library/api/download", json={"magnet": MAGNET}, headers=HTTPS)
    assert r.status_code == 200
    assert eng.added == [MAGNET]
    assert eng.wanted == [None]
    assert eng.pinned == [], "a download must not pin"


def test_download_records_the_label(ctx):
    c, _eng, tmp = ctx
    c.post("/library/api/download",
           json={"magnet": MAGNET,
                 "label": {"metaId": "m1", "name": "Placeholder", "type": "movie"}},
           headers=HTTPS)
    assert labels.load(str(tmp))[IH]["name"] == "Placeholder"


def test_download_without_a_label_stores_none(ctx):
    c, _, tmp = ctx
    c.post("/library/api/download", json={"magnet": MAGNET}, headers=HTTPS)
    assert labels.load(str(tmp)) == {}


def test_download_rejects_a_non_magnet(ctx):
    c, eng, _ = ctx
    r = c.post("/library/api/download", json={"magnet": "https://example.com/x.torrent"},
               headers=HTTPS)
    assert r.status_code == 400
    assert eng.added == []


def test_download_maps_a_malformed_magnet_to_400(tmp_path, monkeypatch):
    """`engine.add` runs lt.parse_magnet_uri, which raises on a magnet that passes the prefix check
    but is otherwise garbage. Unhandled that is a 500 — an operator error reported as a server
    fault."""
    eng = FakeEngine(add_error=RuntimeError("invalid magnet uri"))
    c = _signed_in(tmp_path, monkeypatch, eng)
    r = c.post("/library/api/download", json={"magnet": "magnet:?xt=nonsense"}, headers=HTTPS)
    assert r.status_code == 400
    assert eng.pinned == []


def test_a_download_is_not_subject_to_the_pin_disk_guard(tmp_path, monkeypatch):
    """The guard belongs to pinning, which promises never to evict; a download promises nothing of
    the sort. Streaming has never been gated on free space either, and a download is the same
    operation -- so an engine that would refuse a PIN must not refuse a download."""
    eng = FakeEngine(pin_error=PinSpaceError(needed=1100, free=500))
    c = _signed_in(tmp_path, monkeypatch, eng)
    r = c.post("/library/api/download", json={"magnet": MAGNET}, headers=HTTPS)
    assert r.status_code == 200
    assert eng.wanted == [None] and eng.pinned == []


def test_download_without_an_engine_is_503(tmp_path, monkeypatch):
    monkeypatch.setattr(lib.certcheck, "cert_san", lambda p: "DNS:stremio.example.com")
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    s = Settings(library_ui=True, cache_root=str(tmp_path))
    c = TestClient(create_app(settings=s, engine=None), base_url="https://testserver")
    c.post("/library/api/session", json={"authKey": "good"}, headers=HTTPS)
    assert c.post("/library/api/download", json={"magnet": MAGNET},
                  headers=HTTPS).status_code == 503


def test_remove_unpins_and_drops_the_torrent(ctx):
    c, eng, _ = ctx
    r = c.post("/library/api/remove", json={"infoHash": IH}, headers=HTTPS)
    assert r.status_code == 200
    assert eng.unpinned == [IH] and eng.removed == [IH]


def test_remove_drops_the_label(ctx):
    c, _, tmp = ctx
    c.post("/library/api/download",
           json={"magnet": MAGNET, "label": {"name": "Placeholder"}}, headers=HTTPS)
    c.post("/library/api/remove", json={"infoHash": IH}, headers=HTTPS)
    assert labels.load(str(tmp)) == {}


def test_remove_rejects_a_bad_infohash(ctx):
    c, eng, _ = ctx
    for bad in ("../../etc", "not-hex", "", "a" * 41):
        assert c.post("/library/api/remove", json={"infoHash": bad},
                      headers=HTTPS).status_code == 400
    assert eng.removed == []


def test_remove_cannot_delete_outside_the_cache_root(tmp_path, monkeypatch):
    """The name comes from the TORRENT, which the operator did not author. A torrent whose name is
    a traversal must not steer the delete out of cache_root — the same class of hole that was fixed
    on the /hlsv2 routes in 1.3.7."""
    outside = tmp_path.parent / "must-survive.txt"
    outside.write_text("do not delete me", encoding="utf-8")
    root = tmp_path / "cache"
    root.mkdir()
    eng = FakeEngine(names={f"..{chr(92)}must-survive.txt": IH, "../must-survive.txt": IH})
    c = _signed_in(root, monkeypatch, eng)
    r = c.post("/library/api/remove", json={"infoHash": IH}, headers=HTTPS)
    assert r.status_code == 200          # the torrent is still stopped
    assert outside.exists(), "remove escaped cache_root and deleted a file outside it"


def test_remove_refuses_to_delete_a_protected_name(tmp_path, monkeypatch):
    """A torrent named `pins.json` must not let a remove take the pin registry with it."""
    root = tmp_path / "cache"
    root.mkdir()
    (root / "pins.json").write_text("[]", encoding="utf-8")
    eng = FakeEngine(names={"pins.json": IH})
    c = _signed_in(root, monkeypatch, eng)
    c.post("/library/api/remove", json={"infoHash": IH}, headers=HTTPS)
    assert (root / "pins.json").exists(), "remove deleted a PROTECTED file"


def test_remove_reclaims_the_partfile_and_resume_record(tmp_path, monkeypatch):
    """A torrent leaves more than its directory. libtorrent keeps a `.<infohash>.parts` holding
    file beside the data and a fast-resume record under `.resume/`. Deleting only the directory
    left both — and the partfile is not small: one on a real box held 30 GB after a pinned
    download, so Remove reclaimed almost nothing and the disk stayed full."""
    root = tmp_path / "cache"
    (root / ".resume").mkdir(parents=True)
    (root / "a-download").mkdir()
    (root / "a-download" / "f.bin").write_bytes(b"x" * 64)
    part = root / f".{IH}.parts"
    part.write_bytes(b"y" * 4096)
    resume = root / ".resume" / f"{IH}.fastresume"
    resume.write_bytes(b"z" * 32)

    eng = FakeEngine(names={"a-download": IH})
    c = _signed_in(root, monkeypatch, eng)
    assert c.post("/library/api/remove", json={"infoHash": IH},
                  headers=HTTPS).status_code == 200
    assert not (root / "a-download").exists(), "the data directory survived"
    assert not part.exists(), "the .parts holding file survived — most of the data lives there"
    assert not resume.exists(), "the fast-resume record survived"


def test_remove_stops_the_torrent_before_deleting(tmp_path, monkeypatch):
    """Files must not be deleted underneath a running torrent."""
    root = tmp_path / "cache"
    root.mkdir()
    eng = FakeEngine()
    c = _signed_in(root, monkeypatch, eng)
    c.post("/library/api/remove", json={"infoHash": IH}, headers=HTTPS)
    assert eng.removed == [IH], "engine.remove was not called, so the torrent kept running"
    assert eng.unpinned == [IH], "it stays pinned, so the evictor would still refuse to touch it"


def test_remove_tolerates_a_missing_partfile(tmp_path, monkeypatch):
    """Most torrents have no partfile at all; its absence is not an error."""
    root = tmp_path / "cache"
    root.mkdir()
    c = _signed_in(root, monkeypatch, FakeEngine())
    assert c.post("/library/api/remove", json={"infoHash": IH},
                  headers=HTTPS).status_code == 200


# --- what of the torrent a download actually asks for -----------------------------------------
# Choosing one episode used to fetch the whole season pack: the endpoint never told the engine
# which file was picked, so a pin meant every file in the torrent.

def _download(c, body):
    return c.post("/library/api/download", json=body, headers=HTTPS)


def test_series_download_asks_for_the_chosen_episode(ctx):
    c, eng, _ = ctx
    r = _download(c, {"magnet": MAGNET,
                      "label": {"name": "Show", "type": "series", "season": 1, "episode": 5}})
    assert r.status_code == 200
    assert eng.wanted == [{"season": 1, "episode": 5}]


def test_an_explicit_file_index_beats_the_label(ctx):
    """An addon that points at one file inside a pack is authoritative; the label is the fallback."""
    c, eng, _ = ctx
    r = _download(c, {"magnet": MAGNET, "fileIdx": 4,
                      "label": {"name": "Show", "type": "series", "season": 1, "episode": 5}})
    assert r.status_code == 200
    assert eng.wanted == [{"fileIdx": 4}]


def test_a_film_still_asks_for_the_whole_torrent(ctx):
    """No season/episode means nothing to narrow to, and half a film is worse than all of it."""
    c, eng, _ = ctx
    r = _download(c, {"magnet": MAGNET, "label": {"name": "Some Film", "type": "movie"}})
    assert r.status_code == 200
    assert eng.wanted == [None]


# --- pinning is manual, and the only way anything becomes kept ---------------------------------

def test_pin_is_a_separate_deliberate_act(ctx):
    c, eng, _ = ctx
    r = c.post("/library/api/pin", json={"infoHash": IH}, headers=HTTPS)
    assert r.status_code == 200
    assert eng.pinned == [IH]


def test_pin_still_answers_the_disk_guard_flatly(tmp_path, monkeypatch):
    """The guard belongs to pinning, because a pin is the promise that can overrun a disk: the
    evictor may not reclaim it. Same flat body as /{ih}/pin, so the two cannot drift."""
    eng = FakeEngine(pin_error=PinSpaceError(needed=1100, free=500))
    c = _signed_in(tmp_path, monkeypatch, eng)
    r = c.post("/library/api/pin", json={"infoHash": IH}, headers=HTTPS)
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "insufficient_space" and "detail" not in body


def test_unpin_keeps_the_files(ctx):
    """Unpin is not Remove. It stops keeping the title; the bytes stay and stay playable, and the
    evictor may now reclaim them like any other cache."""
    c, eng, _ = ctx
    r = c.post("/library/api/unpin", json={"infoHash": IH}, headers=HTTPS)
    assert r.status_code == 200
    assert eng.unpinned == [IH] and eng.removed == []


def test_pin_requires_a_session(tmp_path, monkeypatch):
    monkeypatch.setattr(lib.certcheck, "cert_san", lambda p: "DNS:stremio.example.com")
    s = Settings(library_ui=True, cache_root=str(tmp_path))
    c = TestClient(create_app(settings=s, engine=FakeEngine()), base_url="https://testserver")
    assert c.post("/library/api/pin", json={"infoHash": IH}, headers=HTTPS).status_code == 401
