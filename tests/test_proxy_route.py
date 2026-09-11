"""/proxy end to end against a local upstream: headers in, bytes and status out, playlists
rewritten, the destination rule and the stock redirect limit enforced."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.proxy import dest

BLOB = bytes(range(256)) * 64            # 16 KiB; byte N is N % 256
HOME = ("192.168.1.20", 50000)            # a client on the home network
OUTSIDE = ("203.0.113.9", 50000)          # a client from the internet (TEST-NET-3)
TOKEN = "h=X-Token%3Aabc"


class _Upstream(BaseHTTPRequestHandler):
    """A stand-in for an addon's CDN: /blob wants a token header and honours Range, /list.m3u8
    names its own origin, and /hopN redirects to /hop(N+1) until /hop9 redirects to /blob."""

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
        if self.path.startswith("/blob"):
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
            return
        if self.path.startswith("/list.m3u8"):
            origin = f"http://{self.headers['Host']}"
            body = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="/k"\n'
                    f"{origin}/abs.ts\n/root.ts\nrel.ts\nhttps://cdn.example/x.ts\n").encode()
            self._send(200, body, "application/vnd.apple.mpegurl")
            return
        if self.path.startswith("/hop"):
            n = int(self.path.removeprefix("/hop"))
            self._send(302, extra={"Location": f"/hop{n + 1}" if n < 9 else "/blob"})
            return
        self._send(404, b"nope", "text/plain")


@pytest.fixture()
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _opts(srv, *extra: str) -> str:
    return "&".join((f"d=http%3A%2F%2F127.0.0.1%3A{srv.server_address[1]}", *extra))


def _client(peer=HOME) -> TestClient:
    return TestClient(create_app(), client=peer)


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
    port = upstream.server_address[1]
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/list.m3u8")
    assert r.status_code == 200
    root = f"/proxy/d=http%3A%2F%2F127.0.0.1%3A{port}&h=X-Token%3Aabc"
    assert r.text.splitlines() == [
        "#EXTM3U",
        f'#EXT-X-KEY:METHOD=AES-128,URI="{root}/k"',
        f"{root}/abs.ts",
        f"{root}/root.ts",
        "rel.ts",
        "/proxy/d=https%3A%2F%2Fcdn.example&h=X-Token%3Aabc/x.ts",
    ]
    assert r.headers["accept-ranges"] == "none"


def test_four_redirects_are_followed_and_a_fifth_is_an_error(upstream):
    ok = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/hop6")  # hop6..hop9 = four, then /blob
    assert ok.status_code == 200
    assert ok.content == BLOB
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/hop0").status_code == 502


def test_an_internet_client_cannot_reach_a_private_address(upstream):
    assert _client(OUTSIDE).get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 403
    assert upstream.seen == []


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


@pytest.mark.parametrize("path", ["/proxy/", "/proxy/h=X%3A1/x", "/proxy/d=ftp%3A%2F%2Fx/y"])
def test_malformed_options_are_a_400(path):
    assert _client().get(path).status_code == 400


def test_an_unreachable_upstream_is_a_502():
    assert _client().get("/proxy/d=http%3A%2F%2F127.0.0.1%3A9/x").status_code == 502
