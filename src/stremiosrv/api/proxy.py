"""GET/HEAD /proxy/<opts>/<path>: the stock server's proxy for addon HTTP streams.

stremio-video routes a stream through it whenever the addon sets `behaviorHints.proxyHeaders` -- the
request and response headers that stream needs -- and stremio-core builds the same URL for external
players. The server fetches `d` + path with those headers and relays the answer; an HLS playlist is
rewritten so its segments come back through the proxy too. There was no such route before 1.6.7:
nginx answered with the web player's index.html and those streams never played.

Limits the stock proxy does not have, because this server may face the internet: at most
MAX_CONCURRENT proxied requests at once (each holds a worker thread while its upstream is slow, and
enough of them would stall every other route), and a playlist is read, decompressed and rewritten
within fixed sizes (a small compressed body, or many short lines, could otherwise grow without
bound).
"""
from __future__ import annotations

import http.client
import threading
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
# An `r` header may not set these: they describe the bytes on the wire, which the proxy frames.
_FRAMING = frozenset({"content-length", "transfer-encoding", "connection"})
MAX_PLAYLIST_BYTES = 4 << 20  # read from upstream, and again once decompressed
MAX_REWRITTEN_CHARS = 16 << 20  # the rewritten playlist
MAX_CONCURRENT = 16
CHUNK = 64 << 10

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


class _Slot:
    """One of the MAX_CONCURRENT places, given back exactly once: by whatever finishes with the
    upstream, or -- when Starlette drops a streaming body it never started, the client having left
    first -- by that body's finalizer."""

    def __init__(self, places: threading.BoundedSemaphore) -> None:
        self._places = places
        self._held = True
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if not self._held:
                return
            self._held = False
        self._places.release()


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


def _home_client(request: Request) -> bool:
    """Whether this client is on the home network: the same rule, and the same operator setting
    (STREMIOSRV_LIBRARY_ADDON_ALLOW), that the library addon applies."""
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


def _response_headers(resp: http.client.HTTPResponse, o: opts.ProxyOpts) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in RELAY_RESPONSE:
        value = resp.getheader(name)
        if value is not None:
            out[name] = value
    for name, value in o.res_headers:
        if name.lower() not in _FRAMING:
            out[name.lower()] = value
    return out


def _decoded(body: bytes, encoding: str) -> bytes | None:
    """A playlist the upstream compressed anyway, decompressed to at most MAX_PLAYLIST_BYTES. None
    when it would be larger (a compression bomb), is cut short, carries anything after its end, or
    is not what it claims to be."""
    enc = encoding.strip().lower()
    if enc in ("", "identity"):
        return body
    if enc not in ("gzip", "deflate"):
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
        text = playlist.rewrite(decoded.decode("utf-8", "surrogateescape"), o,
                                limit=MAX_REWRITTEN_CHARS)
    except playlist.TooLarge:
        return Response(status_code=502, content=b"playlist too large")
    headers.pop("content-length", None)
    headers["accept-ranges"] = "none"
    return Response(content=text.encode("utf-8", "surrogateescape"), status_code=resp.status,
                    headers=headers)


@router.api_route("/proxy/{rest:path}", methods=["GET", "HEAD"])
def proxy(rest: str, request: Request) -> Response:
    """`rest` is the decoded path and unusable here; the options come from the raw path."""
    raw = (request.scope.get("raw_path") or b"").decode("latin-1")
    parsed = opts.parse(raw[len("/proxy/"):]) if raw.startswith("/proxy/") else None
    if parsed is None:
        return Response(status_code=400, content=b"bad proxy options")
    places = _slots
    if not places.acquire(blocking=False):
        return Response(status_code=503, content=b"too many proxied requests")
    slot = _Slot(places)
    try:
        return _proxied(request, *parsed, slot)
    except BaseException:
        slot.release()
        raise


def _proxied(request: Request, o: opts.ProxyOpts, path: str, slot: _Slot) -> Response:
    """Everything after admission. Gives `slot` back on every path except a streamed body, which
    takes it over."""
    query = request.url.query
    url = o.dest + path + (f"?{query}" if query else "")
    try:
        resp, conn = upstream.open_url(url, request.method, _request_headers(request, o),
                                       _home_client(request))
    except dest.Refused:
        slot.release()
        return Response(status_code=403, content=b"destination not allowed")
    except (OSError, http.client.HTTPException, upstream.TooManyRedirects,
            upstream.BadUpstream):
        slot.release()
        return Response(status_code=502, content=b"upstream unreachable")
    headers = _response_headers(resp, o)
    if request.method == "HEAD":
        _abandon(resp, conn, slot)
        return Response(status_code=resp.status, headers=headers)
    if playlist.is_playlist(path, headers.get("content-type", "")):
        try:
            return _playlist(resp, headers, o)
        finally:
            _abandon(resp, conn, slot)

    def relay() -> Iterator[bytes]:
        try:
            while chunk := resp.read(CHUNK):
                yield chunk
        except (OSError, http.client.HTTPException):
            return  # the upstream died mid-body: end the stream; the player re-requests
        finally:
            _abandon(resp, conn, slot)

    body = relay()
    weakref.finalize(body, _abandon, resp, conn, slot)  # a body Starlette never starts
    return StreamingResponse(body, status_code=resp.status, headers=headers)
