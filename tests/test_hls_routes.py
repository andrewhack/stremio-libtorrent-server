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


# --- HEAD parity. FastAPI does not add HEAD to a GET route (bare Starlette does), so every hlsv2
# route answered 405 while the byte-range route answered 206. A client that probes a URL with HEAD
# before playing it saw a hard failure on the transcode path only.


def _app_client(monkeypatch):
    from fastapi.testclient import TestClient

    from stremiosrv.api import hls
    from stremiosrv.app import create_app

    monkeypatch.setattr(hls, "probe_media", lambda url: {"format": {"name": "matroska"}, "streams": []})
    return TestClient(create_app())


def test_head_is_accepted_on_probe(monkeypatch):
    c = _app_client(monkeypatch)
    r = c.head("/hlsv2/probe", params={"mediaURL": "http://x/y"})
    assert r.status_code == 200
    assert r.content == b""  # HEAD carries headers only


def test_head_matches_get_headers_on_probe(monkeypatch):
    """Same headers as GET is the actual contract — a HEAD that lies about content-type is worse
    than a 405, because the client believes it."""
    c = _app_client(monkeypatch)
    g = c.get("/hlsv2/probe", params={"mediaURL": "http://x/y"})
    h = c.head("/hlsv2/probe", params={"mediaURL": "http://x/y"})
    assert g.status_code == h.status_code == 200
    assert h.headers.get("content-type") == g.headers.get("content-type")


def test_head_is_accepted_on_the_playlist_and_segment_routes(monkeypatch):
    """Both reach their handler rather than the router's 405: no transcoder is wired in this app,
    so 503 is the handler talking. What matters is that it is not 405."""
    c = _app_client(monkeypatch)
    for path in ("/hlsv2/job1/master.m3u8", "/hlsv2/job1/video0.m4s"):
        r = c.head(path, params={"mediaURL": "http://x/y"})
        assert r.status_code != 405, path


class _FakeConv:
    def __init__(self):
        self.stopped = []

    def stop(self, job_id):
        self.stopped.append(job_id)

    def job_dir(self, job_id):
        import pathlib

        return pathlib.Path("/nonexistent")

    def job_file(self, job_id, filename):
        return self.job_dir(job_id) / filename


def test_head_cannot_tear_down_a_transcode_job():
    """HEAD is defined as safe, and /destroy is the one route here whose GET has a side effect. It
    is left off the HEAD list, so a HEAD falls through to the segment route and 404s instead —
    what matters is that stop() is never reached. Asserted as behaviour, not as a status code: the
    catch-all /{job_id}/{filename} also matches this path, so the code alone would not prove it."""
    from fastapi.testclient import TestClient

    from stremiosrv.app import create_app

    app = create_app()
    conv = _FakeConv()
    app.state.converter = conv
    c = TestClient(app)

    c.head("/hlsv2/job1/destroy")
    assert conv.stopped == []          # HEAD must not destroy anything

    assert c.get("/hlsv2/job1/destroy").status_code == 200
    assert conv.stopped == ["job1"]    # ...while GET still does
