"""/proxy end to end against a local upstream: headers in, bytes and status out, playlists
rewritten within their limits, the destination rule, the stock redirect limit, the concurrency cap
with its places kept for home, and the refusal of requests that come back to this server."""
from __future__ import annotations

import gc
import gzip
import http.client
import socket
import threading
import time
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
from stremiosrv.proxy import upstream as upstream_mod

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

    def _late(self, seconds: float, answer) -> None:
        """Answer after `seconds`, if the proxy is still there to hear it."""
        time.sleep(seconds)
        try:
            answer()
        except OSError:  # the proxy gave up first and hung up
            pass

    def _drip(self, head: bytes, piece: bytes, times: int, gap: float) -> None:
        """Send `head`, then `piece` every `gap` seconds, `times` times."""
        try:
            self.wfile.write(head)
            for _ in range(times):
                time.sleep(gap)
                self.wfile.write(piece)
        except OSError:
            pass

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
        elif p.startswith("/xgz.m3u8"):
            self._send(200, gzip.compress(SMALL_PLAYLIST), "application/vnd.apple.mpegurl",
                       {"Content-Encoding": "x-gzip"})
        elif p.startswith("/zl.m3u8"):
            self._send(200, zlib.compress(SMALL_PLAYLIST), "application/vnd.apple.mpegurl",
                       {"Content-Encoding": "deflate"})
        elif p.startswith("/bomb.m3u8"):
            body = gzip.compress(b"#EXTM3U\n" + b"#" * 5000 + b"\n")
            self._send(200, body, "application/vnd.apple.mpegurl", {"Content-Encoding": "gzip"})
        elif p.startswith("/bad.m3u8"):
            self._send(200, b"not gzip at all", "application/vnd.apple.mpegurl",
                       {"Content-Encoding": "gzip"})
        elif p.startswith("/badurl.m3u8"):
            self._send(200, b"#EXTM3U\nhttp://[::1/x\n", "application/vnd.apple.mpegurl")
        elif p.startswith("/hop"):
            n = int(p.removeprefix("/hop"))
            self._send(302, extra={"Location": f"/hop{n + 1}" if n < 9 else "/blob"})
        elif p.startswith("/badipv6"):
            self._send(302, extra={"Location": "http://[::1/x"})
        elif p.startswith("/badport"):
            self._send(302, extra={"Location": "http://127.0.0.1:99999/x"})
        elif p.startswith("/m405"):
            self._send(405, b"not here", "text/plain")
        elif p.startswith("/folded"):
            # send_header writes the value as given, so this is a real obs-fold on the wire
            self._send(200, BLOB, "video/mp4\r\n ; x=1")
        elif p.startswith("/echo"):
            self._send(200, b"ok", "text/plain")
        elif p.startswith("/page.html"):
            self._send(200, b"<script>document.title='x'</script>", "text/html")
        elif p.startswith("/trickle"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", "16")
            self.end_headers()
            self.wfile.write(b"a" * 8)
            self.wfile.flush()
            time.sleep(1.5)
            self.wfile.write(b"b" * 8)
        elif p.startswith("/bare301"):
            self._send(301, b"", "text/plain")  # a redirect that names no Location
        elif p.startswith("/stall"):
            self._late(1.5, lambda: self._send(200, b"late", "text/plain"))
        elif p.startswith("/drip-headers"):
            # a status line, then one header line that never ends, a byte every 0.1 s
            self._drip(b"HTTP/1.1 200 OK\r\n", b"x", 15, 0.1)
        elif p.startswith("/drip.m3u8"):
            # no Content-Length: the playlist ends when the connection does
            self._drip(b"HTTP/1.0 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\n\r\n"
                       b"#EXTM3U\n", b"/seg.ts\n", 8, 0.2)
        elif p.startswith("/slowhop"):
            n = int(p.removeprefix("/slowhop"))
            self._late(0.3, lambda: self._send(302, extra={
                "Location": f"/slowhop{n + 1}" if n < 3 else "/blob"}))
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


def _pools(monkeypatch, total: int, outside: int) -> tuple[threading.BoundedSemaphore, ...]:
    """Swap in small pools: (every place, the internet clients' share of them)."""
    pools = threading.BoundedSemaphore(total), threading.BoundedSemaphore(outside)
    monkeypatch.setattr(proxy_api, "_slots", pools[0])
    monkeypatch.setattr(proxy_api, "_outside_slots", pools[1])
    return pools


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


def test_a_folded_upstream_header_is_unfolded(upstream):
    """http.client keeps an obs-fold as CR LF; relayed like that, the ASGI server would drop the
    whole response. RFC 9112 5.2: replace it with a space."""
    r = _client().get(f"/proxy/{_opts(upstream)}/folded")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4 ; x=1"
    assert r.content == BLOB


@pytest.mark.parametrize(("raw", "sent"), [
    ("video/mp4", "video/mp4"),
    ("video/mp4\r\n ; x=1", "video/mp4 ; x=1"),
    ("a\r\n\tb", "a b"),
    ("a\r\nb", None),
    ("a\nb", None),
    ("a\rb", None),
])
def test_only_values_without_line_breaks_are_relayed(raw, sent):
    assert proxy_api._relayable(raw) == sent


def test_every_answer_is_sandboxed(upstream):
    """Served from the web player's own origin, a relayed page must not run there -- it could read
    the viewer's Stremio sign-in (final review of 1.6.7). Media and fetch loads ignore the policy."""
    page = _client().get(f"/proxy/{_opts(upstream)}/page.html")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == "sandbox"
    head = _client().head(f"/proxy/{_opts(upstream, TOKEN)}/blob")
    assert head.headers["content-security-policy"] == "sandbox"
    listing = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/list.m3u8")
    assert listing.headers["content-security-policy"] == "sandbox"


def test_r_cannot_act_on_this_origin(upstream):
    """Cookies, stored data, the page's policy and redirects belong to the web player's origin."""
    forced = ("r=Set-Cookie%3Asid%3Dx", "r=Content-Security-Policy%3Ascript-src%20*",
              "r=Clear-Site-Data%3A%22storage%22", "r=Refresh%3A0%3Burl%3Dhttps%3A%2F%2Fx.example")
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN, *forced)}/blob")
    assert r.status_code == 200
    assert "set-cookie" not in r.headers
    assert "clear-site-data" not in r.headers
    assert "refresh" not in r.headers
    assert r.headers["content-security-policy"] == "sandbox"


def test_r_cannot_turn_a_relayed_3xx_into_a_redirect(upstream):
    """open_url follows every redirect that names a Location, so a 3xx comes back only when it
    names none. Set by `r`, one would send the viewer from this origin to anywhere (1.6.7
    re-review, N2)."""
    forced = "r=Location%3Ahttps%3A%2F%2Fx.example%2F"
    r = _client().get(f"/proxy/{_opts(upstream, forced)}/bare301", follow_redirects=False)
    assert r.status_code == 301
    assert "location" not in r.headers


def test_the_body_is_relayed_as_it_arrives(upstream):
    """read(n) would wait for all n bytes: a slow upstream must reach the player at once, and the
    thread come back soon after the viewer leaves (final review of 1.6.7)."""
    conn = http.client.HTTPConnection("127.0.0.1", upstream.server_address[1], timeout=10)
    conn.request("GET", "/trickle")
    resp = conn.getresponse()
    body = proxy_api._relay(resp, conn, proxy_api._Slot(()))
    started = time.monotonic()
    first = next(body)
    waited = time.monotonic() - started
    rest = b"".join(body)
    assert waited < 1.0
    assert first
    assert set(first) == {ord("a")}
    assert first + rest == b"a" * 8 + b"b" * 8


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


@pytest.mark.parametrize("path", ["/gz.m3u8", "/xgz.m3u8", "/zl.m3u8"])
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
    monkeypatch.setattr(proxy_api, "MAX_REWRITTEN_BYTES", 200)
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/list.m3u8").status_code == 502


def test_a_playlist_with_an_unparseable_url_is_a_502_not_a_crash(upstream):
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/badurl.m3u8").status_code == 502


def test_a_forced_mpegurl_type_turns_the_rewrite_on(upstream):
    """`r` applies before the playlist test, as in stock, so an addon can declare a playlist (stock
    matches a lowercase `content-type` only; this matches the name in any case)."""
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


@pytest.mark.parametrize("origin", [
    "https://evil.example", "null", "http://[::1",
    "http://203.0.113.7:8080",       # a public address is not the home network
    "ftp://192.168.1.50",            # not a web page
    "http://elsewhere.example:8080", # another name, and no SERVER_URL naming it
])
def test_a_page_on_another_site_is_judged_like_an_internet_client(upstream, origin):
    """Every origin can read /proxy answers (CORS is open, as in stock), so a web page a home viewer
    opens must not read the LAN through them (owner's decision, 2026-09-12)."""
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob", headers={"Origin": origin})
    assert r.status_code == 403
    assert upstream.seen == []


@pytest.mark.parametrize("origin", [
    "https://web.stremio.com", "https://app.strem.io", "http://testserver",
    "http://testserver:8080",        # this server's host on another port (1.6.9)
    "https://testserver:12470",      # ...or scheme
    "http://192.168.1.50:8080",      # a player opened on a home-network address
    "http://100.101.102.103:8080",   # ...over a mesh VPN (carrier-grade NAT space)
    "http://[fd00::5]:8080",         # ...or an IPv6 unique-local address
])
def test_the_stremio_web_app_and_this_servers_own_pages_keep_the_home_rule(upstream, origin):
    """The bundled player is often opened on another address than the one it streams from --
    `http://<home address>:8080` pointed at `https://<name>:12470` -- and a cross-origin request
    carries its page's Origin (1.6.9)."""
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob", headers={"Origin": origin})
    assert r.status_code == 200


def test_a_page_on_server_urls_host_keeps_the_home_rule(upstream):
    """`http://<name>:8080` streaming from SERVER_URL's `https://<name>:12470` (1.6.9)."""
    s = Settings(server_url="https://stremio.example.com:12470/")
    url = f"/proxy/{_opts(upstream, TOKEN)}/blob"
    assert _client(settings=s).get(
        url, headers={"Origin": "http://stremio.example.com:8080"}).status_code == 200
    assert _client(settings=s).get(
        url, headers={"Origin": "http://elsewhere.example"}).status_code == 403


def test_an_unparseable_server_url_names_no_page(upstream):
    s = Settings(server_url="http://[::1")
    r = _client(settings=s).get(f"/proxy/{_opts(upstream, TOKEN)}/blob",
                                headers={"Origin": "http://stremio.example.com"})
    assert r.status_code == 403


def test_a_pages_address_is_judged_by_the_home_networks_setting(upstream):
    """STREMIOSRV_LIBRARY_ADDON_ALLOW decides which page addresses are home, as it does for
    clients (1.6.9)."""
    s = Settings(library_addon_allow="192.168.0.0/16")
    url = f"/proxy/{_opts(upstream, TOKEN)}/blob"
    assert _client(settings=s).get(
        url, headers={"Origin": "http://192.168.7.7:8080"}).status_code == 200
    assert _client(settings=s).get(
        url, headers={"Origin": "http://10.0.0.5:8080"}).status_code == 403


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
    total, _outside = _pools(monkeypatch, 1, 1)
    assert total.acquire(blocking=False)  # another request holds the only place
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 503
    assert upstream.seen == []
    total.release()
    assert _client().get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 200


def test_internet_clients_leave_places_for_home(upstream, monkeypatch):
    """With the internet clients' share taken, another internet request is turned away while a
    home one still gets in -- and the refusal is counted as an internet one."""
    total, outside = _pools(monkeypatch, 2, 1)
    assert outside.acquire(blocking=False)  # an internet client holds the whole share...
    assert total.acquire(blocking=False)    # ...and its place among all of them
    before = proxy_api.refused()
    url = f"/proxy/{_opts(upstream, TOKEN)}/blob"
    assert _client(OUTSIDE).get(url).status_code == 503
    assert _client(HOME).get(url).status_code == 200
    assert proxy_api.refused() == {**before, "internet": before["internet"] + 1}


def test_an_internet_client_turned_away_keeps_no_place(upstream, monkeypatch):
    """Its share had room but every place was taken: the share's place it took must come back."""
    total, outside = _pools(monkeypatch, 1, 1)
    assert total.acquire(blocking=False)
    assert _client(OUTSIDE).get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 503
    assert outside.acquire(blocking=False)


def test_refusals_show_in_stats(upstream, monkeypatch):
    total, _outside = _pools(monkeypatch, 1, 1)
    assert total.acquire(blocking=False)
    c = _client()
    before = c.get("/stats.json").json()["proxyRefused"]
    assert c.get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 503
    assert c.get("/stats.json").json()["proxyRefused"] == {**before, "home": before["home"] + 1}


def test_every_answer_gives_its_place_back(upstream, monkeypatch):
    """One place in each pool: a request that kept one would turn the next into a 503, and a place
    given back twice would make a BoundedSemaphore raise."""
    _pools(monkeypatch, 1, 1)
    c = _client()
    url = f"/proxy/{_opts(upstream, TOKEN)}"
    assert c.get(f"{url}/blob").status_code == 200                  # streamed
    assert c.head(f"{url}/blob").status_code == 200                 # HEAD
    assert c.get(f"{url}/list.m3u8").status_code == 200             # playlist
    assert c.get(f"{url}/bad.m3u8").status_code == 502              # playlist refused
    assert _client(OUTSIDE).get(f"{url}/blob").status_code == 403   # destination refused
    assert _client(OUTSIDE).get(f"{url}/blob").status_code == 403   # ...both its places came back
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


def test_places_are_given_back_once_however_often_released():
    total, outside = threading.BoundedSemaphore(1), threading.BoundedSemaphore(1)
    assert outside.acquire(blocking=False)
    assert total.acquire(blocking=False)
    slot = proxy_api._Slot((outside, total))
    slot.release()
    slot.release()  # a second real release would make the BoundedSemaphores raise ValueError
    assert outside.acquire(blocking=False)
    assert total.acquire(blocking=False)
    assert not outside.acquire(blocking=False)
    assert not total.acquire(blocking=False)


@pytest.mark.parametrize("path", [
    "/proxy/", "/proxy/h=X%3A1/x", "/proxy/d=ftp%3A%2F%2Fx/y",
    "/proxy/d=http%3A%2F%2Fexample.com%3A99999/x", "/proxy/d=http%3A%2F%2F%5B%3A%3A1/y",
])
def test_malformed_options_are_a_400(path):
    assert _client().get(path).status_code == 400


def test_an_unreachable_upstream_is_a_502():
    assert _client().get("/proxy/d=http%3A%2F%2F127.0.0.1%3A9/x").status_code == 502


@pytest.fixture()
def quick(monkeypatch):
    """Half a second instead of the real 30."""
    monkeypatch.setattr(upstream_mod, "DEADLINE", 0.5)


@pytest.mark.parametrize("path", [
    "/stall",         # accepts, then says nothing
    "/drip-headers",  # a header line that never ends, a byte at a time
    "/drip.m3u8",     # a playlist that keeps arriving, with no length to end it
    "/slowhop0",      # redirects that each answer in time, but not all of them together
])
def test_an_upstream_that_does_not_answer_in_time_gets_a_504(upstream, quick, path):
    """A per-read timeout never fires on an upstream that sends a byte now and then; the deadline
    bounds the whole answer, and a playlist cut short is never served (1.6.9)."""
    started = time.monotonic()
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}{path}")
    assert r.status_code == 504
    assert r.content == b"upstream too slow"
    assert time.monotonic() - started < 2.5


def test_a_tls_handshake_that_never_finishes_gets_a_504(quick):
    """Inside wrap_socket the handshake would be out of the deadline's reach."""
    silent = socket.create_server(("127.0.0.1", 0))  # connects, never answers a ClientHello
    try:
        port = silent.getsockname()[1]
        started = time.monotonic()
        r = _client().get(f"/proxy/d=https%3A%2F%2F127.0.0.1%3A{port}/x")
        assert r.status_code == 504
        assert time.monotonic() - started < 2.5
    finally:
        silent.close()


def test_a_body_that_trickles_past_the_deadline_is_relayed_whole(upstream, quick):
    """The deadline ends with the headers: a film streams for as long as it lasts."""
    r = _client().get(f"/proxy/{_opts(upstream)}/trickle")  # 8 bytes, 1.5 s, 8 more
    assert r.status_code == 200
    assert r.content == b"a" * 8 + b"b" * 8


def test_a_playlist_that_arrives_in_time_is_still_rewritten(upstream, quick):
    r = _client().get(f"/proxy/{_opts(upstream, TOKEN)}/list.m3u8")
    assert r.status_code == 200
    assert r.text.startswith("#EXTM3U")


def test_a_504_gives_its_place_back(upstream, quick, monkeypatch):
    _pools(monkeypatch, 1, 1)
    c = _client()
    assert c.get(f"/proxy/{_opts(upstream, TOKEN)}/stall").status_code == 504
    assert c.get(f"/proxy/{_opts(upstream, TOKEN)}/blob").status_code == 200
