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

from stremiosrv.torrent.engine import Engine, Handle  # noqa: E402  (after importorskip)

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


def _make_seed(ses, tmp_path, port_dir="data"):
    """Create a tiny single-file torrent and add it as an immediate seed, auto_managed ON — i.e. the
    exact pre-fix state. Returns the raw libtorrent handle."""
    root = tmp_path / port_dir
    root.mkdir()
    f = root / "movie.bin"
    f.write_bytes(b"x" * 65536)  # 64 KiB
    fs = lt.file_storage()
    lt.add_files(fs, str(f))
    ct = lt.create_torrent(fs, 16384)  # 16 KiB pieces
    lt.set_piece_hashes(ct, str(root.parent))
    ti = lt.torrent_info(ct.generate())
    p = lt.add_torrent_params()
    p.ti = ti
    p.save_path = str(root.parent)
    p.flags |= AUTO_MANAGED | lt.torrent_flags.seed_mode  # managed + already-a-seed
    return ses.add_torrent(p)


def test_handle_pause_de_manages_and_sticks_on_real_lt(tmp_path):
    """A genuinely auto_managed seed, after Handle.pause(), must be taken OUT of auto-management and
    STAY paused — the auto-manager must not resume it (that was the still-uploading bug)."""
    ses = _quiet_session(6890)
    try:
        h = _make_seed(ses, tmp_path)
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
        ses.remove_torrent(h)


def test_engine_add_produces_non_auto_managed(tmp_path):
    """Engine.add() must add torrents already OUT of auto-management, so the seed policy's pause()
    is honored. Uses a bare infohash (no network needed — the handle just sits idle)."""
    eng = Engine(listen_port=6891, cache_root=str(tmp_path))
    try:
        h = eng.add("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        time.sleep(1)
        flags = h.raw().status().flags
        assert not (flags & AUTO_MANAGED), "Engine.add() must clear auto_managed on the params"
    finally:
        eng.shutdown()
