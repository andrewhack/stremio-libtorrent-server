import json
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
    # two oversize entries; one is pinned -> only the unpinned one is evicted
    import os
    import time

    from stremiosrv import cache
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


# --- sparse-file accounting. libtorrent pre-allocates the FULL torrent up front, so `st_size` counts
# bytes that have not been downloaded. On 2026-08-24 a 64%-complete 86.2 GiB torrent was reported as
# `cacheUsed: 92598768617` while `du` measured 56 GiB on disk — the evictor was acting on a number
# 31 GiB larger than the disk agreed with.


def test_real_size_ignores_undownloaded_bytes():
    """A sparse file must be charged for what it occupies, not what it will eventually be."""
    from stremiosrv.cache import _real_size

    class St:  # 86 GiB apparent, 56 GiB allocated
        st_size = 92_598_768_617
        st_blocks = 60_129_542_144 // 512

    assert _real_size(St()) == 60_129_542_144


def test_real_size_does_not_round_small_files_up_to_a_block():
    """Allocation rounds up; a 500-byte file must not be billed 4 KiB. Under-counting is the safe
    direction for a budget whose job is predicting disk pressure."""
    from stremiosrv.cache import _real_size

    class St:
        st_size = 500
        st_blocks = 8  # one 4 KiB block

    assert _real_size(St()) == 500


def test_real_size_falls_back_to_apparent_without_st_blocks():
    """Windows `os.stat_result` has no `st_blocks`; the dev machine must not crash on it."""
    from stremiosrv.cache import _real_size

    class St:
        st_size = 1234

    assert _real_size(St()) == 1234


def test_scan_cache_reports_allocated_not_apparent(tmp_path):
    """End-to-end on a real sparse file: this is the bug as the evictor actually sees it."""
    import pytest

    f = tmp_path / "torrent.mkv"
    with open(f, "wb") as fh:
        fh.truncate(64 * 1024 * 1024)  # 64 MiB apparent
        fh.seek(0)
        fh.write(b"x" * 4096)          # a few KiB actually written
    if getattr(os.stat(f), "st_blocks", None) is None:
        pytest.skip("no st_blocks on this platform (Windows); helper covered by unit tests above")
    size = next(i["size"] for i in scan_cache(str(tmp_path)) if i["name"] == "torrent.mkv")
    assert size < 8 * 1024 * 1024, f"sparse file charged {size} bytes of its 64 MiB apparent size"


# Transcode output lives under <cache_root>/transcode, which scan_cache skips because "transcode" is
# in PROTECTED. That made a disk fill invisible: segments accumulated while cacheUsed still read
# under budget and nothing in /stats.json accounted for the gap.

def test_usage_reports_transcode_separately(tmp_path):
    from stremiosrv.cache import usage

    (tmp_path / "movie").mkdir()
    (tmp_path / "movie" / "a.mkv").write_bytes(b"x" * 4096)
    seg = tmp_path / "transcode" / "job1"
    seg.mkdir(parents=True)
    (seg / "seg0.m4s").write_bytes(b"y" * 8192)

    u = usage(str(tmp_path), budget=1_000_000)
    assert u["transcodeUsed"] > 0, "transcode footprint still invisible in /stats.json"
    assert u["cacheUsed"] > 0
    # Kept apart on purpose: folding transcode into cacheUsed would show the cache over budget
    # while the evictor correctly refused to reclaim a protected directory.
    assert u["transcodeUsed"] not in (u["cacheUsed"],) or u["cacheUsed"] != u["transcodeUsed"]


def test_usage_reports_zero_transcode_before_anything_has_transcoded(tmp_path):
    from stremiosrv.cache import usage

    assert usage(str(tmp_path), budget=1_000_000)["transcodeUsed"] == 0


def test_transcode_is_still_never_evictable(tmp_path):
    """Regression guard: reporting it must not have made it a deletion candidate."""
    seg = tmp_path / "transcode" / "job1"
    seg.mkdir(parents=True)
    (seg / "seg0.m4s").write_bytes(b"y" * 8192)
    assert [i["name"] for i in scan_cache(str(tmp_path))] == []


# --- cache-root ownership -----------------------------------------------------------------
# On 2026-09-03 a second container was started against production's cache directory with a
# smaller budget. Sixty seconds later its evictor took the shared cache to almost nothing:
# every entry read [no-handle]/unserved, because a freshly started server holds no libtorrent
# handles for files it did not download, and pins.json was empty. Nothing in the code made that
# mistake survivable, so the guard belongs here rather than in a runbook.

def _foreign_claim(tmp_path, token="the-other-container", host="some-other-box"):
    """A claim that genuinely belongs to another machine. write_owner stamps this host, and a
    same-host claim is now taken over as our own previous process, so tests that mean "a rival"
    have to say so explicitly."""
    from stremiosrv import cache as c
    c.write_owner(str(tmp_path), token=token)
    p = tmp_path / c.OWNER_FILE
    rec = json.loads(p.read_text())
    rec["host"] = host
    p.write_text(json.dumps(rec))


def test_evictor_may_run_on_a_free_root(tmp_path):
    from stremiosrv import cache as c
    may, other = c.evictor_may_run(str(tmp_path), stale_after=300)
    assert may is True and other is None


def test_evictor_refuses_a_root_another_server_is_holding(tmp_path):
    from stremiosrv import cache as c
    _foreign_claim(tmp_path)
    may, other = c.evictor_may_run(str(tmp_path), stale_after=300)
    assert may is False
    assert other and other["token"] == "the-other-container"


def test_evictor_takes_over_a_root_whose_owner_stopped(tmp_path):
    """A stale claim must never wedge eviction forever — a container that died holds no lock."""
    from stremiosrv import cache as c
    _foreign_claim(tmp_path, token="a-container-that-is-gone")
    rec = json.loads((tmp_path / c.OWNER_FILE).read_text())
    rec["heartbeat"] = time.time() - 4000
    (tmp_path / c.OWNER_FILE).write_text(json.dumps(rec))
    may, other = c.evictor_may_run(str(tmp_path), stale_after=300)
    assert may is True and other is None


def test_evictor_may_run_when_the_claim_is_its_own(tmp_path):
    """Re-entering with our own token (a heartbeat, not a rival) is not a conflict."""
    from stremiosrv import cache as c
    c.write_owner(str(tmp_path))
    may, other = c.evictor_may_run(str(tmp_path), stale_after=300)
    assert may is True and other is None


def test_owner_file_is_protected_from_eviction():
    from stremiosrv import cache as c
    assert c.OWNER_FILE in c.PROTECTED


def test_run_evictor_deletes_nothing_when_another_server_holds_the_root(tmp_path, monkeypatch):
    """The incident, in miniature: a big cache, a small budget, and a rival already in charge.

    Without the guard this call empties the directory — that is exactly what took a live cache
    to almost nothing. run_evictor returns before its loop when the claim is refused, so this
    does not hang.
    """
    from stremiosrv import cache as c
    d = tmp_path / "a-title-this-server-never-downloaded"
    d.mkdir()
    (d / "payload").write_bytes(b"x" * 5000)
    _foreign_claim(tmp_path, token="production-container")
    sleeps = []

    def fake_sleep(_n):
        sleeps.append(1)
        if len(sleeps) >= 2:
            raise SystemExit  # two cycles is enough; the loop no longer exits on its own

    monkeypatch.setattr(c.time, "sleep", fake_sleep)
    try:
        c.run_evictor(str(tmp_path), budget=100, interval=1)
    except SystemExit:
        pass
    assert d.exists(), "evictor deleted another server's cache despite a live claim"


def test_evictor_sleeps_once_per_pass(tmp_path, monkeypatch):
    """The loop slept at the top AND the bottom, so the real period was 2x the interval it
    logged. Three sleeps must therefore cover two eviction passes, not one."""
    from stremiosrv import cache as c
    passes, sleeps = [], []

    def fake_sleep(n):
        sleeps.append(n)
        if len(sleeps) >= 3:
            raise SystemExit

    monkeypatch.setattr(c.time, "sleep", fake_sleep)
    monkeypatch.setattr(c, "evict_once", lambda *a, **k: passes.append(1) or {"deleted": []})
    try:
        c.run_evictor(str(tmp_path), budget=10**9, interval=7)
    except SystemExit:
        pass
    assert sleeps == [7, 7, 7]
    assert len(passes) == 2, f"3 sleeps covered {len(passes)} pass(es) — loop is sleeping twice"


def _mk(root, name, size, age=10_000):
    """A cache entry `size` bytes and `age` seconds old (old enough to clear any mtime grace)."""
    import os as _os
    p = root / name
    if name.startswith(".") and name.endswith(".parts"):
        p.write_bytes(b"x" * size)
    else:
        p.mkdir()
        inner = p / "payload"
        inner.write_bytes(b"x" * size)
        t0 = time.time() - age
        _os.utime(inner, (t0, t0))  # scan_cache walks the tree: the newest file dates the entry
    t = time.time() - age
    _os.utime(p, (t, t))
    return p


def _pins(root, entries):
    (root / "pins.json").write_text(json.dumps(entries), encoding="utf-8")


IH = "a" * 40


def test_pinned_torrent_is_protected_without_a_live_handle(tmp_path):
    """A pin must survive its libtorrent handle. `pinned_names()` needs a loaded torrent WITH
    metadata, so with no engine — or before metadata lands — the pin protected nothing."""
    from stremiosrv import cache as c
    keep = _mk(tmp_path, "Pinned Title", 4000)
    drop = _mk(tmp_path, "Unpinned Title", 4000)
    _pins(tmp_path, [{"infoHash": IH, "name": "Pinned Title"}])
    res = c.evict_once(str(tmp_path), budget=1000)
    names = {d["name"] for d in res["deleted"]}
    assert "Unpinned Title" in names and not drop.exists()
    assert keep.exists() and "Pinned Title" not in names


def test_partfile_of_a_pinned_torrent_is_protected(tmp_path):
    """`.<infohash>.parts` is its own scan entry whose name can never match a torrent name, so it
    was always [no-handle] and always evictable — deleting a live torrent's partial pieces."""
    from stremiosrv import cache as c
    part = _mk(tmp_path, f".{IH}.parts", 3000)
    _mk(tmp_path, "Pinned Title", 4000)
    _mk(tmp_path, "Unpinned Title", 4000)
    _pins(tmp_path, [{"infoHash": IH, "name": "Pinned Title"}])
    c.evict_once(str(tmp_path), budget=1000)
    assert part.exists(), "partfile of a pinned torrent was evicted out from under it"


def test_evicting_a_torrent_takes_its_partfile_with_it(tmp_path):
    """The other direction: a reclaimed torrent must not leave its holding file behind."""
    from stremiosrv import cache as c

    class Eng:
        def recent_names(self, grace): return set()
        def pinned_names(self): return set()
        def name_to_hash(self): return {"Doomed Title": IH}
        def remove(self, ih): pass

    _mk(tmp_path, "Doomed Title", 8000)
    # Recent, so the mtime grace protects it from being selected on its own: only co-eviction
    # with its torrent can remove it, which is exactly what this asserts.
    part = _mk(tmp_path, f".{IH}.parts", 3000, age=5)
    c.evict_once(str(tmp_path), budget=1000, engine=Eng())
    assert not part.exists(), "orphaned partfile survived its torrent"


def test_pin_recorded_with_no_name_is_still_protected(tmp_path):
    """`pin()` stores `h.name() if h.has_metadata() else ""`, and nothing ever backfills it — so a
    magnet pinned the moment it was added carries an empty name for the life of the pin. Observed
    live: the first successful library download recorded `"name": ""`. Without resolving it, the
    durable protection is inert for precisely the pins made from a fresh magnet, which is all of
    them. The name index, written whenever resume data is saved, knows the answer.
    """
    from stremiosrv import cache as c
    keep = _mk(tmp_path, "Pinned But Unnamed", 4000)
    drop = _mk(tmp_path, "Unpinned Title", 4000)
    _pins(tmp_path, [{"infoHash": IH, "name": ""}])
    c.save_name_index(str(tmp_path), {"Pinned But Unnamed": IH})
    res = c.evict_once(str(tmp_path), budget=1000)
    names = {d["name"] for d in res["deleted"]}
    assert "Unpinned Title" in names and not drop.exists()
    assert keep.exists() and "Pinned But Unnamed" not in names


def test_partfile_of_a_nameless_pin_is_protected_too(tmp_path):
    from stremiosrv import cache as c
    part = _mk(tmp_path, f".{IH}.parts", 3000)
    _mk(tmp_path, "Pinned But Unnamed", 4000)
    _mk(tmp_path, "Unpinned Title", 4000)
    _pins(tmp_path, [{"infoHash": IH, "name": ""}])
    c.save_name_index(str(tmp_path), {"Pinned But Unnamed": IH})
    c.evict_once(str(tmp_path), budget=1000)
    assert part.exists()


def test_evictor_takes_over_its_own_previous_process(tmp_path):
    """A restart mints a new token, so the container's own dead claim looked like a rival and
    locked the survivor out. Observed live: "already claimed by another server (pid 12)" logged by
    the only server there was. Same host means the claim is ours to take, whatever the token."""
    from stremiosrv import cache as c
    c.write_owner(str(tmp_path), token="our-previous-process")
    may, other = c.evictor_may_run(str(tmp_path), stale_after=300)
    assert may is True and other is None


def test_a_claim_from_elsewhere_is_still_respected(tmp_path):
    """The takeover above must not swallow the case the guard exists for."""
    from stremiosrv import cache as c
    _foreign_claim(tmp_path)
    may, other = c.evictor_may_run(str(tmp_path), stale_after=300)
    assert may is False and other["host"] == "some-other-box"


def test_a_refused_evictor_keeps_trying(tmp_path, monkeypatch):
    """Refusing was a one-shot `return`, so a claim that went stale a minute later never got
    picked up -- eviction stayed off for the life of the process. It must re-check each cycle."""
    from stremiosrv import cache as c
    _foreign_claim(tmp_path)
    sleeps = []

    def fake_sleep(n):
        sleeps.append(n)
        if len(sleeps) >= 3:
            raise SystemExit

    monkeypatch.setattr(c.time, "sleep", fake_sleep)
    try:
        c.run_evictor(str(tmp_path), budget=10**9, interval=5)
    except SystemExit:
        pass
    assert len(sleeps) == 3, "gave up instead of re-checking the claim"
