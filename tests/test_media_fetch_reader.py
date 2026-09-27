import re

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


def test_reader_applies_ticket_headers_to_the_outbound_fetch(monkeypatch):
    # C1: a ticket carrying request headers (a proxied mediaURL's `h=` options) must have them
    # reach the upstream fetch -- otherwise an authenticated CDN/debrid stream would 401/403.
    from stremiosrv.proxy import upstream
    media_fetch.reset()

    class FakeResp:
        status = 200
        def getheader(self, n, d=None):
            return d
        def read1(self, n):
            return b""
        def close(self): pass

    class FakeConn:
        def close(self): pass

    seen = {}
    def fake_open(url, method, headers, home, deadline):
        seen["headers"] = headers
        return FakeResp(), FakeConn()
    monkeypatch.setattr(upstream, "open_url", fake_open)

    t = media_fetch.register("https://cdn.example/v.mkv", True, (("Authorization", "tok"),))
    r = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}")
    assert r.status_code == 200
    assert seen["headers"]["Authorization"] == "tok"
    assert seen["headers"]["user-agent"] == "Mozilla/5.0"  # the default is kept, not replaced


def test_reader_stream_ends_gracefully_on_a_mid_stream_failure(monkeypatch):
    # M1: a plain (non-playlist) stream whose upstream drops mid-body must end the stream
    # gracefully instead of propagating an OSError through ASGI -- mirrors proxy.py::_relay's own
    # `except (OSError, http.client.HTTPException): return`.
    from stremiosrv.proxy import upstream
    media_fetch.reset()
    closed = {"resp": False, "conn": False}

    class FakeResp:
        status = 200
        def __init__(self):
            self._n = 0
        def getheader(self, n, d=None):
            return d
        def read1(self, n):
            self._n += 1
            if self._n == 1:
                return b"partial-bytes"
            raise ConnectionResetError("peer closed the connection")
        def close(self):
            closed["resp"] = True

    class FakeConn:
        def close(self):
            closed["conn"] = True

    def fake_open(url, method, headers, home, deadline):
        return FakeResp(), FakeConn()
    monkeypatch.setattr(upstream, "open_url", fake_open)

    t = media_fetch.register("https://cdn.example/v.mkv", False)
    r = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}")
    assert r.status_code == 200
    assert r.content == b"partial-bytes"
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


def test_reader_keeps_every_segment_of_a_long_playlist(monkeypatch):
    # #2: a playlist with more segments than the historical 64-ticket cap must not evict its own
    # earliest segments during the rewrite burst -- ffmpeg fetches them in order, and an evicted
    # ticket answers 404. Every segment ticket must still resolve after the whole playlist is
    # rewritten, including the first (which the old cap evicted before ffmpeg reached it).
    from stremiosrv.proxy import upstream
    media_fetch.reset()
    n = 100  # over the old cap of 64 (so this bit the bug before); well under the current cap
    body_bytes = b"#EXTM3U\n" + b"".join(
        f"#EXTINF:1,\nhttps://cdn.example/seg{i}.ts\n".encode() for i in range(n))

    class FakeResp:
        status = 200
        def getheader(self, k, d=None):
            return "application/vnd.apple.mpegurl" if k.lower() == "content-type" else d
        def read(self, *a):
            return body_bytes
        def close(self): pass

    class FakeConn:
        def close(self): pass

    monkeypatch.setattr(upstream, "open_url", lambda *a, **k: (FakeResp(), FakeConn()))

    t = media_fetch.register("https://cdn.example/index.m3u8", False)
    body = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}").text
    tickets = re.findall(r"/_hls-media-read/[^/]+/([A-Za-z0-9_-]+)", body)
    assert len(tickets) == n
    assert all(media_fetch.resolve_ticket(tk) is not None for tk in tickets)
    assert media_fetch.resolve_ticket(tickets[0])[0] == "https://cdn.example/seg0.ts"


def test_reader_carries_playlist_headers_to_same_origin_segments_only(monkeypatch):
    # #3: a proxied playlist's own request headers (its `h=` options, e.g. auth) must ride onto the
    # tickets of SAME-ORIGIN segments, or an authenticated HLS/debrid stream 401s per segment -- but
    # NOT onto a segment on a different ORIGIN: a different host (a third-party CDN), a plain-HTTP
    # downgrade of the HTTPS playlist (which would expose the token to any passive observer), or a
    # different port. Origin = scheme + host + port, not host alone.
    from stremiosrv.proxy import upstream
    media_fetch.reset()
    body_bytes = (b"#EXTM3U\n#EXTINF:1,\nseg-same.ts\n"
                  b"#EXTINF:1,\nhttp://cdn.example/seg-downgrade.ts\n"
                  b"#EXTINF:1,\nhttps://cdn.example:8443/seg-port.ts\n"
                  b"#EXTINF:1,\nhttps://other.cdn/seg-cross.ts\n")

    class FakeResp:
        status = 200
        def getheader(self, k, d=None):
            return "application/vnd.apple.mpegurl" if k.lower() == "content-type" else d
        def read(self, *a):
            return body_bytes
        def close(self): pass

    class FakeConn:
        def close(self): pass

    monkeypatch.setattr(upstream, "open_url", lambda *a, **k: (FakeResp(), FakeConn()))

    t = media_fetch.register("https://cdn.example/hls/index.m3u8", True,
                             (("Authorization", "tok"),))
    body = _client().get(f"{media_fetch.READER_PREFIX}/{media_fetch._SECRET}/{t}").text
    tickets = re.findall(r"/_hls-media-read/[^/]+/([A-Za-z0-9_-]+)", body)
    assert len(tickets) == 4
    same, downgrade, other_port, cross = (media_fetch.resolve_ticket(tk) for tk in tickets)
    assert same[0] == "https://cdn.example/hls/seg-same.ts"
    assert same[2] == (("Authorization", "tok"),)   # same origin -> headers carried
    assert downgrade[0] == "http://cdn.example/seg-downgrade.ts"
    assert downgrade[2] == ()                         # http downgrade -> credential NOT sent cleartext
    assert other_port[0] == "https://cdn.example:8443/seg-port.ts"
    assert other_port[2] == ()                        # different port -> not the same origin
    assert cross[0] == "https://other.cdn/seg-cross.ts"
    assert cross[2] == ()                             # different host -> credential NOT leaked
