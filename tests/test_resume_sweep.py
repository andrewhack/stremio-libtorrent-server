"""The `.resume` sweep: bound the directory without throwing away reusable metadata.

A fast-resume record outlives its data on purpose — it carries the torrent's info-dict, so a
re-play of an evicted title skips the metadata fetch from the swarm entirely. That is why eviction
leaves it behind, and why the sweep is a *bound* rather than a tidy-up: the cutoff is a year, and
anything the server still claims is exempt at any age.
"""
import json
import os
import time

from stremiosrv.cache import sweep_resume

DAY = 86400.0


def _record(root, info_hash, age_days=0.0, suffix=".fastresume"):
    d = os.path.join(root, ".resume")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, info_hash + suffix)
    with open(p, "wb") as f:
        f.write(b"resume-data")
    t = time.time() - age_days * DAY
    os.utime(p, (t, t))
    return p


def _index(root, mapping):
    d = os.path.join(root, ".resume")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.json"), "w", encoding="utf-8") as f:
        json.dump(mapping, f)


H_OLD = "a" * 40
H_NEW = "b" * 40
H_CACHED = "c" * 40
H_KEPT = "d" * 40
H_WANTED = "e" * 40


def test_an_orphan_past_the_cutoff_is_removed(tmp_path):
    p = _record(str(tmp_path), H_OLD, age_days=400)
    res = sweep_resume(str(tmp_path), max_age_days=365)
    assert not os.path.exists(p)
    assert res["removed"] == 1


def test_an_orphan_inside_the_cutoff_is_kept(tmp_path):
    """Its metadata is still the reason a re-play of an evicted title starts without a swarm
    round trip, so age is the only thing that ever retires one."""
    p = _record(str(tmp_path), H_NEW, age_days=364)
    res = sweep_resume(str(tmp_path), max_age_days=365)
    assert os.path.exists(p)
    assert res["removed"] == 0 and res["kept"] == 1


def test_a_record_whose_data_is_still_cached_survives_any_age(tmp_path):
    """The one that makes age-first wrong. A title on disk but not loaded in the session has a
    record as stale as any orphan's — only pinned and wanted torrents are re-added at startup — so
    an age-only rule deletes exactly the metadata most likely to be needed next."""
    (tmp_path / "Some Title").mkdir()
    _index(str(tmp_path), {"Some Title": H_CACHED.upper()})  # index case must not matter
    p = _record(str(tmp_path), H_CACHED, age_days=900)
    res = sweep_resume(str(tmp_path), max_age_days=365)
    assert os.path.exists(p)
    assert res["removed"] == 0 and res["claimed"] == 1


def test_a_kept_torrents_record_survives_any_age(tmp_path):
    (tmp_path / "pins.json").write_text(json.dumps([{"infoHash": H_KEPT.upper(), "name": "x"}]))
    p = _record(str(tmp_path), H_KEPT, age_days=900)
    sweep_resume(str(tmp_path), max_age_days=365)
    assert os.path.exists(p)


def test_a_wanted_torrents_record_survives_any_age(tmp_path):
    (tmp_path / "wanted.json").write_text(json.dumps({H_WANTED: [{"season": 1, "episode": 2}]}))
    p = _record(str(tmp_path), H_WANTED, age_days=900)
    sweep_resume(str(tmp_path), max_age_days=365)
    assert os.path.exists(p)


def test_a_failed_atomic_write_is_reclaimed_but_a_live_one_is_not(tmp_path):
    """Every save writes `<hash>.fastresume.tmp` and renames it. One that outlived the rename by a
    day is a write that raised; one seconds old may be in flight right now."""
    dead = _record(str(tmp_path), H_OLD, age_days=60, suffix=".fastresume.tmp")
    live = _record(str(tmp_path), H_NEW, age_days=0, suffix=".fastresume.tmp")
    res = sweep_resume(str(tmp_path), max_age_days=365)
    assert not os.path.exists(dead)
    assert os.path.exists(live)
    assert res["tmp"] == 1


def test_index_entries_go_with_the_records_they_describe(tmp_path):
    """`index.json` grows in lockstep with the directory and is pruned nowhere — on the live box
    138 of its 141 entries named a directory that no longer existed."""
    (tmp_path / "Still Here").mkdir()
    _index(str(tmp_path), {"Still Here": H_CACHED, "Long Gone": H_OLD, "Never Had One": H_NEW})
    _record(str(tmp_path), H_CACHED, age_days=900)
    _record(str(tmp_path), H_OLD, age_days=400)
    res = sweep_resume(str(tmp_path), max_age_days=365)
    with open(tmp_path / ".resume" / "index.json", encoding="utf-8") as f:
        idx = json.load(f)
    assert set(idx) == {"Still Here"}
    assert res["indexPruned"] == 2


def test_a_retention_of_zero_disables_the_sweep(tmp_path):
    p = _record(str(tmp_path), H_OLD, age_days=4000)
    res = sweep_resume(str(tmp_path), max_age_days=0)
    assert os.path.exists(p)
    assert res == {"removed": 0, "kept": 0, "claimed": 0, "tmp": 0, "indexPruned": 0,
                   "disabled": True}


def test_nothing_outside_the_resume_directory_is_touched(tmp_path):
    """It shares a root with the cache and with every piece of durable state the server owns."""
    old_media = tmp_path / "Ancient Title"
    old_media.mkdir()
    (old_media / "movie.mkv").write_bytes(b"x")
    for p in (old_media, old_media / "movie.mkv"):
        os.utime(p, (time.time() - 4000 * DAY,) * 2)
    (tmp_path / "certificates.pem").write_bytes(b"cert")
    os.utime(tmp_path / "certificates.pem", (time.time() - 4000 * DAY,) * 2)
    sweep_resume(str(tmp_path), max_age_days=365)
    assert (old_media / "movie.mkv").exists()
    assert (tmp_path / "certificates.pem").exists()


def test_a_missing_resume_directory_is_not_an_error(tmp_path):
    assert sweep_resume(str(tmp_path), max_age_days=365)["removed"] == 0


def test_the_evictor_loop_is_what_calls_the_sweep(tmp_path, monkeypatch):
    """A setting that is plumbed but never invoked is invisible — this loop is the only caller,
    and it runs the sweep inside the cache-root claim on purpose."""
    import pytest

    from stremiosrv import cache as c

    calls = []
    monkeypatch.setattr(c, "sweep_resume", lambda root, days: (
        calls.append((root, days)),
        {"removed": 1, "kept": 0, "claimed": 0, "tmp": 0, "indexPruned": 0},
    )[1])
    monkeypatch.setattr(c, "evict_once", lambda *a, **k: {"before": 0, "after": 0, "deleted": []})
    monkeypatch.setattr(c, "evictor_may_run", lambda *a, **k: (True, None))

    class Stop(Exception):
        pass

    # Counted, not keyed on `calls`: a stop condition that depends on the thing under test turns a
    # broken wiring into a hung suite instead of a failed assertion (it did, once).
    sleeps = []

    def fake_sleep(_):
        sleeps.append(1)
        if len(sleeps) >= 3:
            raise Stop

    monkeypatch.setattr(c.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        c.run_evictor(str(tmp_path), budget=1, resume_retention_days=365)
    # Once, not once per pass -- the eviction loop runs every minute and this is a daily job.
    assert calls == [(str(tmp_path), 365)]


def test_the_retention_is_configurable_from_the_environment(monkeypatch):
    from stremiosrv.config import Settings

    assert Settings().resume_retention_days == 365
    monkeypatch.setenv("STREMIOSRV_RESUME_RETENTION_DAYS", "0")
    assert Settings().resume_retention_days == 0
