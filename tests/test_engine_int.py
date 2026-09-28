import os
import time

import pytest

pytestmark = pytest.mark.integration

# engine.py now imports cleanly without libtorrent (lt=None) so the app stays importable; skip these
# real-session tests when the binding is actually absent (dev laptops / CI without libtorrent).
pytest.importorskip("libtorrent")


def _engine_or_skip(port: int):
    try:
        from stremiosrv.torrent.engine import Engine
    except Exception as e:  # noqa: BLE001 — any import failure means 'skip', not 'fail'
        pytest.skip(f"libtorrent unavailable: {e}")
    return Engine(listen_port=port, cache_root="/tmp/st-cache")


def test_listener_binds():
    """The engine must LISTEN for inbound peers (the stock server never does)."""
    eng = _engine_or_skip(6882)
    try:
        time.sleep(1)
        assert eng.listen_port() > 0
    finally:
        eng.shutdown()


def test_add_torrent_gets_metadata():
    magnet = os.environ.get("TEST_MAGNET")
    if not magnet:
        pytest.skip("set TEST_MAGNET to a legal magnet")
    eng = _engine_or_skip(6883)
    try:
        h = eng.add(magnet)
        deadline = time.time() + 90
        while not h.has_metadata() and time.time() < deadline:
            time.sleep(1)
        assert h.has_metadata()
        assert h.torrent_file().num_files() >= 1
    finally:
        eng.shutdown()


def _engine_at(tmp_path, **kw):
    from stremiosrv.torrent.engine import Engine

    # listen_port=0 -> OS-assigned, so these never collide with the fixed-port tests above.
    return Engine(listen_port=0, cache_root=str(tmp_path), **kw)


def test_upnp_and_natpmp_on_by_default(tmp_path):
    """Default leaves automatic router port-mapping on — unchanged behaviour."""
    eng = _engine_at(tmp_path)
    try:
        s = eng._ses.get_settings()
        assert s["enable_upnp"]
        assert s["enable_natpmp"]
    finally:
        eng.shutdown()


def test_enable_upnp_false_disables_upnp_and_natpmp(tmp_path):
    """config.enable_upnp=False must actually reach libtorrent and turn OFF both the UPnP and
    the NAT-PMP port mapper — the one switch governs both."""
    eng = _engine_at(tmp_path, enable_upnp=False)
    try:
        s = eng._ses.get_settings()
        assert not s["enable_upnp"]
        assert not s["enable_natpmp"]
    finally:
        eng.shutdown()
