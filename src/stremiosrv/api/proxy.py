"""GET/HEAD /proxy/<opts>/<path>: the stock server's proxy for addon HTTP streams.

stremio-video routes a stream through it whenever the addon sets `behaviorHints.proxyHeaders` -- the
request and response headers that stream needs -- and stremio-core builds the same URL for external
players. The server fetches `d` + path with those headers and relays the answer; an HLS playlist is
rewritten so its segments come back through the proxy too. There was no such route before 1.6.7:
nginx answered with the web player's index.html and those streams never played.

Limits the stock proxy does not have, because this server may face the internet and serves the web
player on the same origin: every answer is sandboxed, so a page fetched from anywhere cannot run on
that origin; a web page on another site is judged like an internet client; at most MAX_CONCURRENT
proxied requests run at once (each holds a worker thread while its upstream is slow, and enough of
them would stall every other route), of which internet clients may hold only MAX_CONCURRENT_OUTSIDE,
so the home network always finds a place; and a playlist is read, decompressed and rewritten within
fixed sizes (a small compressed body, or many short lines, could otherwise grow without bound).
"""
from __future__ import annotations

import http.client
import re
import threading
import urllib.parse
import weakref
import zlib
from collections.abc import Iterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from stremiosrv.library import netguard
from stremiosrv.proxy import dest, opts, playlist, upstream

router = APIRouter()

# Marks every request we send upstream. One that comes back to this server -- over loopback, the
# container's own address or the public name -- is refused by RefuseOwnRequests below, on every
# route, so the proxy can never reach this server's own origin-only routes.
LOOP_HEADER = "X-Stremiosrv-Proxy"
_LOOP_KEY = LOOP_HEADER.lower().encode("latin-1")
# The client's request headers worth forwarding: stock's list minus the hop-by-hop ones the HTTP
# layer owns, and minus accept-encoding -- the bytes are relayed as they come, so ask for them plain.
FORWARD_REQUEST = ("accept", "accept-language", "range", "if-range", "user-agent")
# Upstream response headers relayed: stock's list minus the hop-by-hop ones and the two the ASGI
# server writes itself (server, date), plus content-encoding so a body compressed anyway stays
# readable.
RELAY_RESPONSE = ("accept-ranges", "content-type", "content-length", "content-range",
                  "last-modified", "etag", "content-encoding")
# An `r` header may not set these. The framing ones describe the bytes on the wire, which the proxy
# frames; the others would act on this origin -- the web player's own -- instead of describing a
# stream: its cookies, its stored data, the page's policy, a redirect (Refresh, or a Location on a
# relayed 3xx, which comes back only when the upstream named none -- 1.6.7 re-review, N2).
_NOT_SETTABLE = frozenset({"content-length", "transfer-encoding", "connection", "set-cookie",
                           "content-security-policy", "clear-site-data", "refresh", "location"})
# Every answer is served from the web player's own origin, where the player keeps the viewer's
# Stremio sign-in, so a page fetched from anywhere must not run there: sandboxed, it gets an origin
# of its own and no scripts. Browsers apply the policy to documents only; media, subtitle and
# fetch/XHR loads -- the player's own -- ignore it (final review of 1.6.7).
SANDBOX = "sandbox"
# Web origins of the official Stremio web app. A page on any other origin -- or one that hides its
# origin ("null") -- is judged like an internet client: every origin can read /proxy answers (CORS
# is open, as in stock), so without this any website a home viewer opens could read the LAN through
# the viewer's own server (owner's decision, 2026-09-12). The bundled player's own requests are
# same-origin and native apps send no Origin, so both keep the home rule.
STREMIO_WEB_ORIGINS = frozenset({"https://web.stremio.com", "https://app.strem.io"})
# http.client keeps an obs-fold -- a header value continued on the next line -- as CR LF plus the
# continuation's leading whitespace. The ASGI servers refuse a value with a line break in it and
# drop the response, so it becomes one space first, as RFC 9112 5.2 says.
_OBS_FOLD = re.compile(r"\r?\n[ \t]+")
MAX_PLAYLIST_BYTES = 4 << 20  # read from upstream, and again once decompressed
MAX_REWRITTEN_BYTES = 16 << 20  # the rewritten playlist
MAX_CONCURRENT = 16
# Internet clients may hold at most this many of the MAX_CONCURRENT places, so a viewer on the home
# network always finds one (owner's decision, 2026-09-12).
MAX_CONCURRENT_OUTSIDE = 12
CHUNK = 64 << 10

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_outside_slots = threading.BoundedSemaphore(MAX_CONCURRENT_OUTSIDE)
_refused_lock = threading.Lock()
_refused = {"home": 0, "internet": 0}


class _Slot:
    """A proxied request's places -- one of the MAX_CONCURRENT and, for an internet client, one of
    the MAX_CONCURRENT_OUTSIDE too -- given back exactly once: by whatever finishes with the
    upstream, or, when Starlette drops a streaming body it never started (the client having left
    first), by that body's finalizer."""

    def __init__(self, places: tuple[threading.BoundedSemaphore, ...]) -> None:
        self._places = places
        self._held = True
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if not self._held:
                return
            self._held = False
        for place in reversed(self._places):
            place.release()


def _admit(home: bool) -> _Slot | None:
    """This client's places, or None when they are taken -- a refusal counted for /stats.json."""
    pools = (_slots,) if home else (_outside_slots, _slots)
    taken: list[threading.BoundedSemaphore] = []
    for pool in pools:
        if not pool.acquire(blocking=False):
            for place in reversed(taken):
                place.release()
            with _refused_lock:
                _refused["home" if home else "internet"] += 1
            return None
        taken.append(pool)
    return _Slot(tuple(taken))


def refused() -> dict[str, int]:
    """For /stats.json: /proxy requests turned away because every place was taken, since start."""
    with _refused_lock:
        return dict(_refused)


def _abandon(resp: http.client.HTTPResponse, conn: http.client.HTTPConnection,
             slot: _Slot) -> None:
    resp.close()
    conn.close()
    slot.release()


class RefuseOwnRequests:
    """ASGI wrapper: refuse, on every route, any request that carries this server's proxy marker.

    Only /proxy sets LOOP_HEADER, on the requests it sends upstream, so one arriving here is a
    proxied request that came back to this server. Answering it would hand this server's own routes,
    the origin-only ones included (the cache list, pins, active streams), to whoever asked the
    proxy: an internet client through the server's public address, and -- behind a reverse proxy,
    where every client counts as home -- anyone at all."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and any(key == _LOOP_KEY for key, _ in scope["headers"]):
            await send({"type": "http.response.start", "status": 508,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": b"proxy loop"})
            return
        await self.app(scope, receive, send)


def _host_of(url: str) -> str:
    """The host a URL names, lowercase; "" when it names none or cannot be parsed."""
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _foreign_page(request: Request) -> bool:
    """Whether a web page on another site than this server or the Stremio web app sent this.

    A page is the server's own when its host -- at any port, over either scheme -- is the host the
    request was sent to, the host SERVER_URL names, or an address on the home network
    (STREMIOSRV_LIBRARY_ADDON_ALLOW). The bundled player is often opened on another address than
    the one it streams from -- `http://<home address>:8080` pointed at SERVER_URL's
    `https://<name>:12470` -- and a cross-origin request carries its page's Origin (1.6.9)."""
    origin = request.headers.get("origin")
    if origin is None or origin in STREMIO_WEB_ORIGINS:
        return False
    try:
        u = urllib.parse.urlsplit(origin)
        host = u.hostname or ""
    except ValueError:
        return True
    if u.scheme not in ("http", "https") or not host:
        return True
    settings = request.app.state.settings
    own = {_host_of("//" + request.headers.get("host", "")), _host_of(settings.server_url)}
    if host in own - {""}:
        return False
    return not netguard.is_allowed(host, netguard.parse_allow(settings.library_addon_allow))


def _home_client(request: Request) -> bool:
    """Whether this request gets the home rule: a client on the home network -- the same rule, and
    the same operator setting (STREMIOSRV_LIBRARY_ADDON_ALLOW), that the library addon applies --
    and not a web page on another site."""
    if _foreign_page(request):
        return False
    peer = request.client.host if request.client else ""
    ip = netguard.client_ip(peer, request.headers.get("x-forwarded-for", ""))
    allow = netguard.parse_allow(request.app.state.settings.library_addon_allow)
    return netguard.is_allowed(ip, allow)


def _request_headers(request: Request, o: opts.ProxyOpts) -> dict[str, str]:
    out = {k: request.headers[k] for k in FORWARD_REQUEST if k in request.headers}
    out["accept-encoding"] = "identity"
    for name, value in o.req_headers:
        out = {k: v for k, v in out.items() if k.lower() != name.lower()}
        out[name] = value
    out[LOOP_HEADER] = "1"
    return out


def _relayable(value: str) -> str | None:
    """An upstream header value as it can be sent on: an obs-fold becomes one space; a value with
    any other line break is dropped."""
    value = _OBS_FOLD.sub(" ", value)
    return None if "\r" in value or "\n" in value else value


def _response_headers(resp: http.client.HTTPResponse, o: opts.ProxyOpts) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in RELAY_RESPONSE:
        raw = resp.getheader(name)
        value = _relayable(raw) if raw is not None else None
        if value is not None:
            out[name] = value
    for name, value in o.res_headers:
        if name.lower() not in _NOT_SETTABLE:
            out[name.lower()] = value
    out["content-security-policy"] = SANDBOX
    return out


def _decoded(body: bytes, encoding: str) -> bytes | None:
    """A playlist the upstream compressed anyway, decompressed to at most MAX_PLAYLIST_BYTES. None
    when it would be larger (a compression bomb), is cut short, carries anything after its end, or
    is not what it claims to be."""
    enc = encoding.strip().lower()
    if enc in ("", "identity"):
        return body
    if enc not in ("gzip", "x-gzip", "deflate"):
        return None
    d = zlib.decompressobj(wbits=zlib.MAX_WBITS | 32)  # a gzip or a zlib header, either one
    try:
        out = d.decompress(body, MAX_PLAYLIST_BYTES + 1)
    except zlib.error:
        return None
    if len(out) > MAX_PLAYLIST_BYTES or d.unconsumed_tail or d.unused_data or not d.eof:
        return None
    return out


def _playlist(resp: http.client.HTTPResponse, headers: dict[str, str],
              o: opts.ProxyOpts) -> Response:
    """Read, decompress and rewrite, each within its limit. The caller closes the upstream."""
    try:
        body = resp.read(MAX_PLAYLIST_BYTES + 1)
    except (OSError, http.client.HTTPException):
        return Response(status_code=502, content=b"upstream unreachable")
    if len(body) > MAX_PLAYLIST_BYTES:
        return Response(status_code=502, content=b"playlist too large")
    decoded = _decoded(body, headers.pop("content-encoding", ""))
    if decoded is None:
        return Response(status_code=502, content=b"undecodable playlist")
    try:
        rewritten = playlist.rewrite(decoded, o, limit=MAX_REWRITTEN_BYTES)
    except playlist.TooLarge:
        return Response(status_code=502, content=b"playlist too large")
    except ValueError:  # a URL in it that cannot be parsed or written back
        return Response(status_code=502, content=b"unusable playlist")
    headers.pop("content-length", None)
    headers["accept-ranges"] = "none"
    return Response(content=rewritten, status_code=resp.status, headers=headers)


def _relay(resp: http.client.HTTPResponse, conn: http.client.HTTPConnection,
           slot: _Slot) -> Iterator[bytes]:
    """The upstream body, passed on as it arrives: read1 returns whatever is there, so a slow
    upstream reaches the player at once and the thread is back soon after the viewer leaves --
    read(n) would wait for all n bytes (final review of 1.6.7)."""
    try:
        while chunk := resp.read1(CHUNK):
            yield chunk
    except (OSError, http.client.HTTPException):
        return  # the upstream died mid-body: end the stream; the player re-requests
    finally:
        _abandon(resp, conn, slot)


@router.api_route("/proxy/{rest:path}", methods=["GET", "HEAD"])
def proxy(rest: str, request: Request) -> Response:
    """`rest` is the decoded path and unusable here; the options come from the raw path."""
    raw = (request.scope.get("raw_path") or b"").decode("latin-1")
    parsed = opts.parse(raw[len("/proxy/"):]) if raw.startswith("/proxy/") else None
    if parsed is None:
        return Response(status_code=400, content=b"bad proxy options")
    home = _home_client(request)
    slot = _admit(home)
    if slot is None:
        return Response(status_code=503, content=b"too many proxied requests")
    try:
        return _proxied(request, *parsed, home, slot)
    except BaseException:
        slot.release()
        raise


def _too_slow() -> Response:
    """The upstream did not answer within upstream.DEADLINE."""
    return Response(status_code=504, content=b"upstream too slow")


def _proxied(request: Request, o: opts.ProxyOpts, path: str, home: bool,
             slot: _Slot) -> Response:
    """Everything after admission. Gives `slot` back on every path except a streamed body, which
    takes it over. The upstream gets upstream.DEADLINE to answer, and to deliver a playlist whole;
    past it the answer is 504 (owner's decision, 2026-09-13)."""
    query = request.url.query
    url = o.dest + path + (f"?{query}" if query else "")
    deadline = upstream.Deadline(upstream.DEADLINE)
    try:
        resp, conn = upstream.open_url(url, request.method, _request_headers(request, o), home,
                                       deadline)
    except dest.Refused:
        slot.release()
        return Response(status_code=403, content=b"destination not allowed")
    except (OSError, http.client.HTTPException, upstream.TooManyRedirects,
            upstream.BadUpstream):  # DeadlinePassed is a TimeoutError, so among the OSErrors
        slot.release()
        if not deadline.stop():
            return _too_slow()
        return Response(status_code=502, content=b"upstream unreachable")
    headers = _response_headers(resp, o)
    if request.method == "HEAD":
        _abandon(resp, conn, slot)
        return Response(status_code=resp.status, headers=headers)
    if playlist.is_playlist(path, headers.get("content-type", "")):
        try:
            answer = _playlist(resp, headers, o)
        finally:
            _abandon(resp, conn, slot)
        return answer if deadline.stop() else _too_slow()  # a playlist cut short is never served
    deadline.stop()  # the headers are in: the body is the viewer's, for as long as it runs
    body = _relay(resp, conn, slot)
    weakref.finalize(body, _abandon, resp, conn, slot)  # a body Starlette never starts
    return StreamingResponse(body, status_code=resp.status, headers=headers)
