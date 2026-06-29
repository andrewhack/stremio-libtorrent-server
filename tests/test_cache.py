import os
import time

from stremiosrv.cache import evict_once, scan_cache, select_evictions


def test_select_none_when_under_budget():
    items = [{"name": "a", "size": 100, "mtime": 1}, {"name": "b", "size": 100, "mtime": 2}]
    assert select_evictions(items, budget=1000) == []


def test_select_oldest_first_until_target():
    items = [
        {"name": "old", "size": 600, "mtime": 1},
        {"name": "mid", "size": 600, "mtime": 2},
        {"name": "new", "size": 600, "mtime": 3},
    ]
    # total 1800, budget 1000 (target 900): drop old+mid -> 600 <= 900
    victims = [v["name"] for v in select_evictions(items, budget=1000)]
    assert victims == ["old", "mid"]


def test_select_skips_in_use():
    items = [{"name": "old", "size": 1000, "mtime": 1}, {"name": "new", "size": 1000, "mtime": 2}]
    victims = [v["name"] for v in select_evictions(items, budget=500, in_use=frozenset({"old"}))]
    assert victims == ["new"]  # oldest is in use -> protected


def test_scan_skips_protected(tmp_path):
    (tmp_path / "certificates.pem").write_bytes(b"x")
    (tmp_path / "movie.mkv").write_bytes(b"y" * 100)
    (tmp_path / "transcode").mkdir()
    names = {i["name"] for i in scan_cache(str(tmp_path))}
    assert "movie.mkv" in names
    assert "certificates.pem" not in names
    assert "transcode" not in names


def test_scan_dir_size(tmp_path):
    d = tmp_path / "show"
    d.mkdir()
    (d / "ep.mkv").write_bytes(b"z" * 500)
    items = {i["name"]: i for i in scan_cache(str(tmp_path))}
    assert items["show"]["size"] == 500


def test_evict_once_keeps_budget_not_everything(tmp_path):
    old = time.time() - 10_000  # older than grace -> evictable
    for i in range(5):
        f = tmp_path / f"f{i}.mkv"
        f.write_bytes(b"x" * 100)
        os.utime(f, (old + i, old + i))
    res = evict_once(str(tmp_path), budget=250, engine=None, grace=300)  # total 500
    remaining = sum(i["size"] for i in scan_cache(str(tmp_path)))
    assert 0 < remaining <= 250          # under budget but NOT wiped out
    assert len(res["deleted"]) >= 1


def test_evict_once_protects_recent(tmp_path):
    for i in range(5):  # just-created files (recent mtime) must be protected
        (tmp_path / f"f{i}.mkv").write_bytes(b"x" * 100)
    res = evict_once(str(tmp_path), budget=100, engine=None, grace=300)  # total 500 > budget
    assert res["deleted"] == []          # all recent -> nothing evicted


def test_evict_skips_pinned_names(tmp_path, monkeypatch):
    from stremiosrv import cache
    # two oversize entries; one is pinned -> only the unpinned one is evicted
    import os
    import time
    old = time.time() - 10_000
    for name in ("pinned-movie", "other-movie"):
        d = tmp_path / name
        d.mkdir()
        f = d / "f"
        f.write_bytes(b"x" * 2_000_000)
        os.utime(f, (old, old))   # file mtime must be old so grace=0 doesn't protect it
        os.utime(d, (old, old))

    class FakeEngine:
        def recent_names(self, grace): return set()
        def name_to_hash(self): return {}
        def pinned_names(self): return {"pinned-movie"}

    removed = []
    monkeypatch.setattr(cache, "_remove", lambda p: removed.append(os.path.basename(p)))
    cache.evict_once(str(tmp_path), budget=1_000_000, engine=FakeEngine(), grace=0)
    assert "pinned-movie" not in removed
    assert "other-movie" in removed
