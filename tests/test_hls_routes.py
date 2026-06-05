from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_hwaccel_profiler_default_none():
    c = TestClient(create_app())
    r = c.get("/hwaccel-profiler")
    assert r.status_code == 200
    assert r.json()["profile"] is None


def test_master_503_without_converter():
    c = TestClient(create_app())
    r = c.get("/hlsv2/abc/master.m3u8", params={"mediaURL": "http://x/0"})
    assert r.status_code == 503


def test_segment_503_without_converter():
    c = TestClient(create_app())
    r = c.get("/hlsv2/abc/seg0.m4s")
    assert r.status_code == 503
