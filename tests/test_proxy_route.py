"""/proxy end to end against a local upstream: headers in, bytes and status out, playlists
rewritten within their limits, the destination rule, the stock redirect limit, the concurrency cap
and the refusal of requests that come back to this server."""
from __future__ import annotations

import gc
import gzip
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from stremiosrv import unmatched
from stremiosrv.api import proxy as proxy_api
from stremiosrv.app import create_app
from stremiosrv.config import Settings
from stremiosrv.proxy import dest

BLOB = bytes(range(256)) * 64            # 16 KiB; byte N is N % 256
HOME = ("192.168.1.20", 50000)            # a client on the home network
OUTSIDE = ("203.0.113.9", 50000)          # a client from the internet (TEST-NET-3)
NGINX = ("127.0.0.1", 50000)              # the production peer: our own nginx
TOKEN = "h=X-Token%3Aabc"
FORCED_MPEGURL = "r=content-type%3Aapplication%2Fvnd.apple.mpegurl"
SMALL_PLAYLIST = b"#EXTM3U\n/root.ts\nrel.ts\n"


class _Upstream(BaseHTTPRequestHandler):
    """A stand-in for an addon's CDN: /blob wants a token header and honours Range, /list.m3u8
    names its own origin, /hopN redirects to /hop(N+1) until /hop9 redirects to /blob, and the
    other paths serve the edge cases their tests name."""

    def log_message(self, *_args) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/octet-stream",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self.server.seen.append((self.command, self.path, self.headers))
        p = self.path
        if p.startswith("/blob"):
            if self.headers.get("X-Token") != "abc":
                self._send(403, b"forbidden", "text/plain")
                return
            rng = self.headers.get("Range")
            if rng:
                start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
                self._send(206, BLOB[start:end + 1], "video/mp4", {
                    "Content-Range": f"bytes {start}-{end}/{len(BLOB)}",
                    "Accept-Ranges": "bytes", "Set-Cookie": "not=relayed"})
                return
            self._send(200, BLOB, "video/mp4", {"Accept-Ranges": "bytes"})
        elif p.startswith("/list.m3u8"):
            origin = f"http://{self.headers['Host']}"
            body = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="/k"\n'
                    f"{origin}/abs.ts\n/root.ts\nrel.ts\nhttps://cdn.example/x.ts\n").encode()
            self._send(200, body, "application/vnd.apple.mpegurl")
        elif p.startswith("/list-noext"):
            self._send(200, SMALL_PLAYLIST, "text/plain")
        elif p.startswith("/gz.m3u8"):
            self._send(200, gzip.compress(SMALL_PLAYLIST), "application/vnd.apple.mpegurl",
                       {"Content-Encoding": "gzip"})
        elif p.startswith("/zl.m3u8"):
            self._send(200, zlib.compress(SMALL_PLAYLIST), "application/vnd.apple.mpegurl",
                       {"Content-Encoding": "deflate"})
        elif p.startswith("/bomb.m3u8"):
            body = gzip.compress(b"#EXTM3U\n" + b"#" * 5000 + b"\n")
            self._send(200, body, "application/vnd.apple.mpegurl", {"Content-Encoding": "gzip"})
        elif p.startswith("/bad.m3u8"):
            self._send(200, b"not gzip at all", "application/vnd.apple.mpegurl",
                       {"Content-Encoding": "gzip"})
        elif p.startswith("/hop"):
            n = int(p.removeprefix("/hop"))
            self._send(302, extra={"Location": f"/hop{n + 1}" if n < 9 else "/blob"})
        elif p.startswith("/badipv6"):
            self._send(302, extra={"Location": "http://[::1/x"})
        elif p.startswith("/badport"):
            self._send(302, extra={"Location": "http://127.0.0.1:99999/x"})
        elif p.startswith("/m405"):
            self._send(405, b"not here", "text/plain")
        elif p.startswith("/echo"):
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"nope", "text/plain")


@pytest.fixture()
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _opts(srv, *extra: str) -> str:
    return "&".join((f"d=http%3A%2F%2F127.0.0.1%3A{srv.server_address[1]}", *extra))


def _root(srv, *extra: str) -> str:
    """Where a rewritten playlist points back: /proxy/ and the options as serialize writes them."""
    return "/proxy/" + _opts(srv, TOKEN, *extra)


def _client(peer: tuple[str, int] = HOME, settings: Settings | None = None) -> TestClient:
    return TestClient(create_app(settings=settings), client=peer)


def test_a_range_request_is_relayed_with_the_addons_headers(upstream):
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob",
                      headers={"Range": "bytes=10-19", "User-Agent": "player/1.0"})
    assert r.status_code == 206
    assert r.content == BLOB[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(BLOB)}"
    assert "set-cookie" not in r.headers
    _method, _path, sent = upstream.seen[-1]
    assert sent["X-Token"] == "abc"
    assert sent["User-Agent"] == "player/1.0"
    assert sent["Accept-Encoding"] == "identity"
    assert sent["Host"] == f"127.0.0.1:{upstream.server_address[1]}"
    assert sent["X-Stremiosrv-Proxy"] == "1"


def test_without_the_addons_header_the_upstream_refusal_is_relayed(upstream):
    assert _client().get(f"/proxy/{_opts(upstream)}/blob").status_code == 403


def test_forced_response_headers_apply_but_never_to_framing(upstream):
    forced = ("r=Content-Type%3Avideo%2Fx-matroska", "r=Content-Length%3A1")
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN, *forced)}/blob")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/x-matroska"
    assert r.headers["content-length"] == str(len(BLOB))
    assert r.content == BLOB


def test_head_is_relayed_without_a_body(upstream):
    r = _client().head(f"/proxy/{_opts(upstream, TOKEN)}/blob")
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(BLOB))
    assert r.content == b""
    assert upstream.seen[-1][0] == "HEAD"


def test_the_query_string_is_forwarded(upstream):
    _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob?token=xyz&n=1")
    assert upstream.seen[-1][1] == "/blob?token=xyz&n=1"


def test_a_playlist_comes_back_rewritten(upstream):
    root = _root(upstream)
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/list.m3u8")
    assert r.status_code == 200
    assert r.text.splitlines() == [
        "#EXTM3U",
        f'#EXT-X-KEY:METHOD=AES-128,URI="{root}/k"',
        f"{root}/abs.ts",
        f"{root}/root.ts",
        "rel.ts",
        "/proxy/d=https%3A%2F%2Fcdn.example&h=X-Token%3Aabc/x.ts",
    ]
    assert r.headers["accept-ranges"] == "none"


@pytest.mark.parametrize("path", ["/gz.m3u8", "/zl.m3u8"])
def test_a_compressed_playlist_is_decoded_then_rewritten(upstream, path):
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}{path}")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.text.splitlines() == ["#EXTM3U", f"{_root(upstream)}/root.ts", "rel.ts"]


def test_a_playlist_that_decompresses_past_the_limit_is_refused(upstream, monkeypatch):
    monkeypatch.setattr(proxy_api, "MAX_PLAYLIST_BYTES", 1024)
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/bomb.m3u8").status_code == 502


def test_a_playlist_that_does_not_decompress_is_refused(upstream):
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/bad.m3u8").status_code == 502


def test_a_playlist_whose_rewrite_passes_the_limit_is_refused(upstream, monkeypatch):
    monkeypatch.setattr(proxy_api, "MAX_REWRITTEN_CHARS", 200)
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/list.m3u8").status_code == 502


def test_a_forced_mpegurl_type_turns_the_rewrite_on(upstream):
    """Stock tests the headers after `r` is applied, so an addon can declare a playlist."""
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN, FORCED_MPEGURL)}/list-noext")
    assert r.status_code == 200
    root = _root(upstream, FORCED_MPEGURL)
    assert r.text.splitlines() == ["#EXTM3U", f"{root}/root.ts", "rel.ts"]


def test_four_redirects_are_followed_and_a_fifth_is_an_error(upstream):
    ok = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/hop6")  # hop6..hop9 = four, then /blob
    assert ok.status_code == 200
    assert ok.content == BLOB
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/hop0").status_code == 502


@pytest.mark.parametrize("path", ["/badipv6", "/badport"])
def test_a_malformed_redirect_is_a_502_not_a_crash(upstream, path):
    assert _client().get(f"/proxy/{_opts(upstream)}{path}").status_code == 502


def test_an_internet_client_cannot_reach_a_private_address(upstream):
    assert _client(OUTSIDE).get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 403
    assert upstream.seen == []


def test_the_production_path_judges_the_forwarded_address(upstream):
    """Behind our nginx the peer is loopback and the client is in X-Forwarded-For."""
    url = f"/proxy/{_opts(upstream, TOKEN)}/blob"
    outside = _client(NGINX).get(url, headers={"X-Forwarded-For": "203.0.113.9"})
    assert outside.status_code == 403
    home = _client(NGINX).get(url, headers={"X-Forwarded-For": "192.168.1.20"})
    assert home.status_code == 200


def test_the_home_network_is_the_operators_allowlist(upstream):
    """STREMIOSRV_LIBRARY_ADDON_ALLOW decides who is home, for /proxy as for the library addon."""
    url = f"/proxy/{_opts(upstream, TOKEN)}/blob"
    narrowed = _client(HOME, Settings(library_addon_allow="10.0.0.0/8")).get(url)
    assert narrowed.status_code == 403
    assert upstream.seen == []
    widened = _client(OUTSIDE, Settings(library_addon_allow="203.0.113.0/24")).get(url)
    assert widened.status_code == 200


def test_every_redirect_hop_is_checked(upstream, monkeypatch):
    """The first hop passes (allowed for the test); the redirect must be judged again."""
    calls = []

    def first_only(address, home_client):
        calls.append(address)
        return len(calls) == 1

    monkeypatch.setattr(dest, "allowed", first_only)
    assert _client(OUTSIDE).get(f"/proxy/{_opts(upstream, TOKEN)}/hop0").status_code == 403
    assert len(upstream.seen) == 1


def test_a_request_that_is_already_ours_is_refused(upstream):
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob",
                      headers={"X-Stremiosrv-Proxy": "1"})
    assert r.status_code == 508
    assert upstream.seen == []


@pytest.mark.parametrize("path", ["/health", "/cache.json", "/active.json"])
def test_a_request_back_to_this_server_is_refused_on_every_route(path):
    """What the marker protects: this server's own routes, reached through its own proxy."""
    assert _client().get(path, headers={"X-Stremiosrv-Proxy": "1"}).status_code == 508


def test_an_addon_host_header_replaces_ours_exactly_once(upstream):
    _client().get(f"/proxy/{_opts(upstream, 'h=host%3Aother.example')}/echo")
    assert upstream.seen[-1][2].get_all("Host") == ["other.example"]


def test_a_relayed_405_is_not_counted_as_a_missing_route(upstream):
    unmatched.reset()
    assert _client().get(f"/proxy/{_opts(upstream)}/m405").status_code == 405
    assert unmatched.snapshot() == {}


def test_requests_past_the_cap_get_a_503(upstream, monkeypatch):
    places = threading.BoundedSemaphore(1)
    monkeypatch.setattr(proxy_api, "_slots", places)
    assert places.acquire(blocking=False)  # another request holds the only place
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 503
    assert upstream.seen == []
    places.release()
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 200


def test_every_answer_gives_its_place_back(upstream, monkeypatch):
    """One place: a request that kept it would turn the next one into a 503, and a place given
    back twice would make the BoundedSemaphore raise."""
    places = threading.BoundedSemaphore(1)
    monkeypatch.setattr(proxy_api, "_slots", places)
    c = _client()
    url = f"/proxy/{_opts(upstream, TOKEN)}"
    assert c.get(f"{url}/blob").status_code == 200                  # streamed
    assert c.head(f"{url}/blob").status_code == 200                 # HEAD
    assert c.get(f"{url}/list.m3u8").status_code == 200             # playlist
    assert c.get(f"{url}/bad.m3u8").status_code == 502              # playlist refused
    assert _client(OUTSIDE).get(f"{url}/blob").status_code == 403   # destination refused
    assert c.get(f"{url}/badport").status_code == 502               # malformed redirect
    assert c.get("/proxy/d=http%3A%2F%2F127.0.0.1%3A9/x").status_code == 502  # unreachable
    assert c.get(f"{url}/blob").status_code == 200


def test_a_body_that_never_starts_still_gives_its_place_back(upstream, monkeypatch):
    """The client left before the body began: Starlette drops the response unstarted, the
    relay's own cleanup never runs, and its finalizer has to give the place back."""
    places = threading.BoundedSemaphore(1)
    monkeypatch.setattr(proxy_api, "_slots", places)
    raw = f"/proxy/{_opts(upstream, TOKEN)}/blob"
    request = Request({"type": "http", "method": "GET", "path": raw, "raw_path": raw.encode(),
                       "query_string": b"", "headers": [], "client": HOME, "app": create_app()})
    response = proxy_api.proxy("", request)
    assert isinstance(response, StreamingResponse)
    assert not places.acquire(blocking=False)  # the unstarted body holds the only place
    del response
    gc.collect()
    assert places.acquire(blocking=False)


def test_a_place_is_given_back_once_however_often_it_is_released():
    places = threading.BoundedSemaphore(1)
    assert places.acquire(blocking=False)
    slot = proxy_api._Slot(places)
    slot.release()
    slot.release()  # a second real release would make the BoundedSemaphore raise ValueError
    assert places.acquire(blocking=False)
    assert not places.acquire(blocking=False)


@pytest.mark.parametrize("path", [
    "/proxy/", "/proxy/h=X%3A1/x", "/proxy/d=ftp%3A%2F%2Fx/y",
    "/proxy/d=http%3A%2F%2Fexample.com%3A99999/x", "/proxy/d=http%3A%2F%2F%5B%3A%3A1/y",
])
def test_malformed_options_are_a_400(path):
    assert _client().get(path).status_code == 400


def test_an_unreachable_upstream_is_a_502():
    assert _client().get("/proxy/d=http%3A%2F%2F127.0.0.1%3A9/x").status_code == 502
