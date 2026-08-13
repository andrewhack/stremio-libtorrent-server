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


# --- eviction diagnostics. A 4K title vanished mid-evening on 2026-08-12 and the log could not say
# whether it had been idle for an hour or was being watched: it recorded only name and size.


class _AgeEngine:
    """Engine stub exposing the three hooks evict_once uses, plus access_ages."""

    def __init__(self, ages=None, hashes=None, recent=()):
        self._ages, self._hashes, self._recent = ages or {}, hashes or {}, set(recent)

    def recent_names(self, grace):
        return self._recent

    def pinned_names(self):
        return set()

    def name_to_hash(self):
        return self._hashes

    def access_ages(self):
        return self._ages

    def remove(self, ih):
        pass


def _oversize(tmp_path, *names, size=2_000_000):
    old = time.time() - 10_000
    for n in names:
        f = tmp_path / n
        f.write_bytes(b"x" * size)
        os.utime(f, (old, old))


def test_eviction_log_carries_infohash_and_age(tmp_path, caplog):
    _oversize(tmp_path, "movie.mkv")
    eng = _AgeEngine(ages={"movie.mkv": 2460.0}, hashes={"movie.mkv": "ab" * 20})
    with caplog.at_level("INFO", logger="stremiosrv.cache"):
        evict_once(str(tmp_path), budget=1000, engine=eng, grace=300)
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "ab" * 20 in line          # which torrent, not just which title
    assert "41m ago" in line          # 2460s -> 41 minutes: a clean reclaim, not a live stream


def test_eviction_log_says_unserved_when_never_requested(tmp_path, caplog):
    """A leftover on disk with no access record must not be reported as freshly served."""
    _oversize(tmp_path, "leftover.mkv")
    with caplog.at_level("INFO", logger="stremiosrv.cache"):
        evict_once(str(tmp_path), budget=1000, engine=_AgeEngine(), grace=300)
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "unserved" in msgs and "no-handle" in msgs


def test_over_budget_with_nothing_evictable_warns(tmp_path, caplog):
    """The silent case: protecting everything is correct, but the cache then sits over budget
    forever and the log used to say nothing at all. Next stop is a full disk."""
    _oversize(tmp_path, "a.mkv", "b.mkv")
    eng = _AgeEngine(recent=("a.mkv", "b.mkv"))
    with caplog.at_level("WARNING", logger="stremiosrv.cache"):
        res = evict_once(str(tmp_path), budget=1000, engine=eng, grace=1800)
    assert res["deleted"] == []
    warn = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warn, "over budget with nothing evictable must be visible"
    assert "nothing is evictable" in warn[0].getMessage()
    assert "grace=1800s" in warn[0].getMessage()


def test_no_warning_when_under_budget(tmp_path, caplog):
    _oversize(tmp_path, "a.mkv")
    with caplog.at_level("WARNING", logger="stremiosrv.cache"):
        evict_once(str(tmp_path), budget=10_000_000, engine=_AgeEngine(recent=("a.mkv",)), grace=1800)
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_no_warning_when_something_was_evicted(tmp_path, caplog):
    _oversize(tmp_path, "keep.mkv", "drop.mkv")
    eng = _AgeEngine(recent=("keep.mkv",))
    with caplog.at_level("WARNING", logger="stremiosrv.cache"):
        res = evict_once(str(tmp_path), budget=2_500_000, engine=eng, grace=1800)
    assert [d["name"] for d in res["deleted"]] == ["drop.mkv"]
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_grace_default_is_long_enough_for_a_4k_player():
    """300s was shorter than a 4K player's gap between range requests, so the title being watched
    could age out of protection while the cache was over budget."""
    from stremiosrv.config import Settings

    assert Settings().cache_evict_grace >= 1800
