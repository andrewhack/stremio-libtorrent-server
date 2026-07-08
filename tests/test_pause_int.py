"""Real-libtorrent proof that stop-seeding actually stops (the auto_managed fix).

These are the tests the fake-handle unit tests CAN'T be: the bug was that `handle.pause()` alone
doesn't stop an `auto_managed` torrent — the session auto-manager resumes it — so it keeps uploading
even though our bookkeeping said paused. A fake handle always honors pause(), which is exactly why the
bug hid. Here we drive a genuine libtorrent session, so the auto-manager is real.

Swarm-free and deterministic: we build a tiny .torrent from a local file and add it in seed_mode, so
the torrent is a complete seed immediately without any network. Skipped where libtorrent is absent.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

lt = pytest.importorskip("libtorrent")

from stremiosrv.torrent.engine import Engine, Handle, should_stop_seeding  # noqa: E402  (after importorskip)

AUTO_MANAGED = lt.torrent_flags.auto_managed
PAUSED = lt.torrent_flags.paused


def _quiet_session(port: int):
    return lt.session({
        "listen_interfaces": f"0.0.0.0:{port}",
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
    })


def _add_managed(ses, tmp_path, hexhash: str):
    """Add a real, auto_managed torrent (bare infohash — metadata isn't needed to exercise the
    pause/auto-manager interaction; the auto-manager still owns and would resume it)."""
    p = lt.add_torrent_params()
    p.info_hashes = lt.info_hash_t(lt.sha1_hash(bytes.fromhex(hexhash)))
    p.save_path = str(tmp_path)
    p.flags |= AUTO_MANAGED
    return ses.add_torrent(p)


def test_handle_pause_de_manages_and_sticks_on_real_lt(tmp_path):
    """A genuinely auto_managed torrent, after Handle.pause(), must be taken OUT of auto-management
    and STAY paused — the auto-manager must not resume it (that was the still-uploading bug)."""
    ses = _quiet_session(6890)
    h = None
    try:
        h = _add_managed(ses, tmp_path, "bb" * 20)
        time.sleep(1)
        assert h.status().flags & AUTO_MANAGED, "precondition: torrent is auto_managed (the bug state)"

        Handle(h).pause()

        st = h.status()
        assert not (st.flags & AUTO_MANAGED), "pause() must clear auto_managed"
        assert st.flags & PAUSED, "torrent must be paused right after pause()"

        time.sleep(3)  # give the auto-manager time to (wrongly) resume it, as in the bug
        st2 = h.status()
        assert not (st2.flags & AUTO_MANAGED), "must STAY out of auto-management"
        assert st2.flags & PAUSED, "must STAY paused — no auto-resume (this is the fix)"
    finally:
        if h is not None:
            ses.remove_torrent(h)


def _make_pack_torrent(tmp_path):
    """A 2-file torrent, each file a whole number of 16 KiB pieces (so no piece straddles two files).
    Returns (torrent_info, content_root)."""
    root = tmp_path / "pack"
    root.mkdir()
    (root / "ep0.bin").write_bytes(b"0" * 32768)  # 2 pieces
    (root / "ep1.bin").write_bytes(b"1" * 32768)  # 2 pieces
    fs = lt.file_storage()
    lt.add_files(fs, str(root))
    ct = lt.create_torrent(fs, 16384)
    lt.set_piece_hashes(ct, str(root.parent))
    return lt.torrent_info(ct.generate()), root


def test_finished_pack_is_finished_not_seeding_on_real_lt(tmp_path):
    """The TV-pack bug, on real libtorrent, deterministic (no network): only file0 is wanted (file1
    priority 0) and only file0's data is on disk. After a recheck libtorrent reports is_finished=True
    (all WANTED data present) but is_seeding=False (file1 missing) — the exact partially-watched-pack
    state that kept uploading. The seed policy (keyed on is_finished) must treat it as complete."""
    ti, root = _make_pack_torrent(tmp_path)
    save = tmp_path / "save"
    (save / "pack").mkdir(parents=True)
    (save / "pack" / "ep0.bin").write_bytes((root / "ep0.bin").read_bytes())  # file0 only
    ses = _quiet_session(6892)
    h = None
    try:
        p = lt.add_torrent_params()
        p.ti = ti
        p.save_path = str(save)
        p.file_priorities = [4, 0]           # file0 wanted, file1 skipped (mirrors focus_file on a pack)
        p.flags &= ~(AUTO_MANAGED | PAUSED)  # run it so it checks the on-disk data
        h = ses.add_torrent(p)
        h.force_recheck()
        for _ in range(80):                  # wait for the recheck to settle (64 KiB -> fast)
            st = h.status()
            if "checking" not in str(st.state).lower() and st.is_finished:
                break
            time.sleep(0.25)
        st = h.status()
        assert st.is_finished, "all WANTED data (file0) present -> is_finished"
        assert not st.is_seeding, "file1 missing -> NOT a full seed (is_seeding False)"
        wrapped = Handle(h)
        assert wrapped.is_finished() is True and wrapped.is_seeding() is False
        assert should_stop_seeding(pinned=False, finished=wrapped.is_finished(), completed_at=1.0,
                                   now=1.0, seed_on_complete=False, max_seed_minutes=0) is True
    finally:
        if h is not None:
            ses.remove_torrent(h)


def test_note_stream_open_resumes_paused_unfinished_torrent(tmp_path):
    """Resume-on-play (real lt): if the seed policy paused a torrent whose focused file still needs
    downloading (e.g. the next episode of a pack), opening a stream must RESUME it, else playback
    stalls on a paused torrent. A bare-infohash add has no data -> is_finished False -> must resume."""
    eng = Engine(listen_port=6893, cache_root=str(tmp_path))
    try:
        h = eng.add("cccccccccccccccccccccccccccccccccccccccc")
        time.sleep(1)
        h.pause()
        assert h.is_paused() is True and h.is_finished() is False
        eng.note_stream_open(h)
        assert h.is_paused() is False, "opening a stream on a paused+unfinished torrent must resume it"
    finally:
        eng.shutdown()


def test_engine_add_produces_non_auto_managed_and_running(tmp_path):
    """Engine.add() must add torrents OUT of auto-management AND not paused. Clearing auto_managed
    alone would strand the torrent paused (libtorrent's default flags are auto_managed|paused, and
    with no auto-manager to start it, it never runs — no metadata, no download). Uses a bare infohash
    (no network needed — we only inspect the flags)."""
    eng = Engine(listen_port=6891, cache_root=str(tmp_path))
    try:
        h = eng.add("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        time.sleep(1)
        flags = h.raw().status().flags
        assert not (flags & AUTO_MANAGED), "Engine.add() must clear auto_managed on the params"
        assert not (flags & PAUSED), "Engine.add() must also clear paused, else the torrent never runs"
    finally:
        eng.shutdown()
