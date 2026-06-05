import os
import time

import pytest

pytestmark = pytest.mark.integration


def _engine_or_skip(port: int):
    try:
        from stremiosrv.torrent.engine import Engine
    except Exception as e:  # libtorrent not installed (e.g. dev laptop)
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
