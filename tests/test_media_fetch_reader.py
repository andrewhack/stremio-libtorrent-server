from fastapi.testclient import TestClient

from stremiosrv.api import media_fetch
from stremiosrv.app import create_app


def _client():
    # Starlette's TestClient defaults request.client.host to the literal string "testclient", not
    # a loopback IP (its own hard-coded default), so _is_own_ffmpeg's loopback check would refuse
    # even the real secret. Pin it to a loopback peer, as tests/test_embedded_ass_reader.py already
    # does for the same reader-route pattern.
    return TestClient(create_app(), client=("127.0.0.1", 50000))


def test_reader_refuses_without_the_secret():
    media_fetch.reset()
    t = media_fetch.register("http://127.0.0.1:9/x.mkv", True)
    r = _client().get(f"{media_fetch.READER_PREFIX}/wrong-secret/{t}")
    assert r.status_code == 404


def test_reader_refuses_through_nginx_forwarded_header():
    media_fetch.reset()
    t = media_fetch.register("http://127.0.0.1:9/x.mkv", True)
    # the real secret, but an X-Forwarded-For means it arrived through nginx, not our own ffmpeg
    r = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}",
                      headers={"x-forwarded-for": "1.2.3.4"})
    assert r.status_code == 404


def test_reader_rewrites_relative_segment_against_playlist_url(monkeypatch):
    # a real fetch would need a network peer; instead stub open_url to return a canned playlist
    from stremiosrv.proxy import upstream
    media_fetch.reset()

    class FakeResp:
        status = 200
        def getheader(self, n, d=None):
            return "application/vnd.apple.mpegurl" if n.lower() == "content-type" else d
        def read(self, *a):
            return b"#EXTM3U\n#EXTINF:1.0,\nseg1.ts\nhttps://cdn.example/abs/seg2.ts\n"
        def close(self): pass

    class FakeConn:
        def close(self): pass

    seen = {}
    def fake_open(url, method, headers, home, deadline):
        seen["url"] = url
        return FakeResp(), FakeConn()
    monkeypatch.setattr(upstream, "open_url", fake_open)

    t = media_fetch.register("https://cdn.example/hls/index.m3u8", False)
    body = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}").text
    # both segments now point back at the reader, and the relative one was absolutised (guarded)
    assert body.count(media_fetch.READER_PREFIX) == 2
    assert "seg1.ts" not in body and "cdn.example" not in body


def test_reader_surfaces_a_refused_hop(monkeypatch):
    # a redirect into the LAN (or any refused destination) makes open_url raise dest.Refused; the
    # reader must turn that into a clean 502, never a 500 or a leaked body.
    from stremiosrv.proxy import dest, upstream
    media_fetch.reset()

    def refuse(*a, **k):
        raise dest.Refused("192.168.1.10")
    monkeypatch.setattr(upstream, "open_url", refuse)

    t = media_fetch.register("https://public.example/v.mkv", False)
    r = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}")
    assert r.status_code == 502


def test_reader_turns_a_mid_read_failure_into_502(monkeypatch):
    # the connect succeeds and headers arrive, but the upstream drops while the playlist body is
    # being read -- must still be a clean 502 (never an unhandled 500), and resp/conn must close.
    from stremiosrv.proxy import upstream
    media_fetch.reset()
    closed = {"resp": False, "conn": False}

    class FakeResp:
        status = 200
        def getheader(self, n, d=None):
            return "application/vnd.apple.mpegurl" if n.lower() == "content-type" else d
        def read(self, *a):
            raise ConnectionResetError("peer closed the connection")
        def close(self):
            closed["resp"] = True

    class FakeConn:
        def close(self):
            closed["conn"] = True

    def fake_open(url, method, headers, home, deadline):
        return FakeResp(), FakeConn()
    monkeypatch.setattr(upstream, "open_url", fake_open)

    t = media_fetch.register("https://cdn.example/hls/index.m3u8", False)
    r = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}")
    assert r.status_code == 502
    assert closed == {"resp": True, "conn": True}


def test_reader_reports_an_oversized_playlist_as_502(monkeypatch):
    # the size guard must actually bite: shrink the cap so a small canned body already overflows it,
    # rather than allocating a multi-MiB body just to cross the real default.
    from stremiosrv.proxy import upstream
    media_fetch.reset()
    monkeypatch.setattr(media_fetch, "_MAX_PLAYLIST_BYTES", 10)

    class FakeResp:
        status = 200
        def getheader(self, n, d=None):
            return "application/vnd.apple.mpegurl" if n.lower() == "content-type" else d
        def read(self, *a):
            return b"#EXTM3U\n" * 5  # well over the shrunk 10-byte cap
        def close(self): pass

    class FakeConn:
        def close(self): pass

    def fake_open(url, method, headers, home, deadline):
        return FakeResp(), FakeConn()
    monkeypatch.setattr(upstream, "open_url", fake_open)

    t = media_fetch.register("https://cdn.example/hls/index.m3u8", False)
    r = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}")
    assert r.status_code == 502
