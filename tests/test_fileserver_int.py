import os
import time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_range_serves_first_bytes():
    magnet = os.environ.get("TEST_MAGNET")
    if not magnet:
        pytest.skip("set TEST_MAGNET to a legal magnet")
    try:
        from stremiosrv.torrent.engine import Engine
    except Exception as e:
        pytest.skip(f"libtorrent unavailable: {e}")
    from stremiosrv.app import create_app

    eng = Engine(listen_port=6884, cache_root="/tmp/st-cache")
    try:
        h = eng.add(magnet)
        deadline = time.time() + 90
        while not h.has_metadata() and time.time() < deadline:
            time.sleep(1)
        assert h.has_metadata()
        ih = h.info_hash()

        client = TestClient(create_app(engine=eng))
        r = client.get(f"/{ih}/0", headers={"Range": "bytes=0-1023"})
        assert r.status_code == 206
        assert r.headers["Accept-Ranges"] == "bytes"
        assert r.headers["Content-Range"].startswith("bytes 0-1023/")
        assert int(r.headers["Content-Length"]) == 1024
        assert len(r.content) == 1024
    finally:
        eng.shutdown()
