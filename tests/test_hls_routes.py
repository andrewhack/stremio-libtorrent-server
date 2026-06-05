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


def test_non_int_idx_does_not_match_serve():
    # regression: paths like /hlsv2/probe must NOT be swallowed by playback's serve route.
    # Before pinning idx to :int this returned 422 (int_parsing); now it 404s and falls through.
    c = TestClient(create_app())
    r = c.get("/somehash/notanint")
    assert r.status_code == 404
