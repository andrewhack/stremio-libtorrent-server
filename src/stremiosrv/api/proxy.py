"""GET/HEAD /proxy/<opts>/<path>: the stock server's proxy for addon HTTP streams.

stremio-video routes a stream through it whenever the addon sets `behaviorHints.proxyHeaders` -- the
request and response headers that stream needs -- and stremio-core builds the same URL for external
players. The server fetches `d` + path with those headers and relays the answer; an HLS playlist is
rewritten so its segments come back through the proxy too. There was no such route before 1.6.7:
nginx answered with the web player's index.html and those streams never played.
"""
from __future__ import annotations

import gzip
import http.client
import zlib

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from stremiosrv.library import netguard
from stremiosrv.proxy import dest, opts, playlist, upstream

router = APIRouter()

# Marks every request we send, so one that comes back to us -- a destination resolving to this
# server's own public address -- is answered once instead of proxying itself until something gives.
LOOP_HEADER = "X-Stremiosrv-Proxy"
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
MAX_PLAYLIST_BYTES = 16 << 20
CHUNK = 64 << 10


def _home_client(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    return netguard.is_allowed(netguard.client_ip(peer, request.headers.get("x-forwarded-for", "")))


def _request_headers(request: Request, o: opts.ProxyOpts) -> dict[str, str]:
    out = {k: request.headers[k] for k in FORWARD_REQUEST if k in request.headers}
    out["accept-encoding"] = "identity"
    for name, value in o.req_headers:
        out = {k: v for k, v in out.items() if k.lower() != name.lower()}
        out[name] = value
    out[LOOP_HEADER] = "1"
    return out


def _response_headers(resp: http.client.HTTPResponse, o: opts.ProxyOpts) -> dict[str, str]:
    out = {}
    for name in RELAY_RESPONSE:
        value = resp.getheader(name)
        if value is not None:
            out[name] = value
    for name, value in o.res_headers:
        if name.lower() not in _FRAMING:
            out[name.lower()] = value
    return out


def _decoded(body: bytes, encoding: str) -> bytes:
    """A playlist the upstream compressed anyway: it has to be read to be rewritten."""
    enc = encoding.lower()
    if enc == "gzip":
        return gzip.decompress(body)
    if enc == "deflate":
        return zlib.decompress(body)
    return body


def _playlist(resp, conn, headers: dict[str, str], o: opts.ProxyOpts) -> Response:
    try:
        body = resp.read(MAX_PLAYLIST_BYTES + 1)
    except (OSError, http.client.HTTPException):
        return Response(status_code=502, content=b"upstream unreachable")
    finally:
        resp.close()
        conn.close()
    if len(body) > MAX_PLAYLIST_BYTES:
        return Response(status_code=502, content=b"playlist too large")
    try:
        body = _decoded(body, headers.pop("content-encoding", ""))
    except (OSError, zlib.error):
        return Response(status_code=502, content=b"undecodable playlist")
    headers.pop("content-length", None)
    headers["accept-ranges"] = "none"
    text = body.decode("utf-8", "surrogateescape")
    return Response(content=playlist.rewrite(text, o).encode("utf-8", "surrogateescape"),
                    status_code=resp.status, headers=headers)


@router.api_route("/proxy/{rest:path}", methods=["GET", "HEAD"])
def proxy(rest: str, request: Request) -> Response:
    """`rest` is the decoded path and unusable here; the options come from the raw path."""
    if request.headers.get(LOOP_HEADER):
        return Response(status_code=508, content=b"proxy loop")
    raw = (request.scope.get("raw_path") or b"").decode("latin-1")
    parsed = opts.parse(raw[len("/proxy/"):]) if raw.startswith("/proxy/") else None
    if parsed is None:
        return Response(status_code=400, content=b"bad proxy options")
    o, path = parsed
    query = request.url.query
    url = o.dest + path + (f"?{query}" if query else "")
    try:
        resp, conn = upstream.open_url(url, request.method, _request_headers(request, o),
                                       _home_client(request))
    except dest.Refused:
        return Response(status_code=403, content=b"destination not allowed")
    except (OSError, http.client.HTTPException, upstream.TooManyRedirects):
        return Response(status_code=502, content=b"upstream unreachable")
    headers = _response_headers(resp, o)
    if request.method == "HEAD":
        resp.close()
        conn.close()
        return Response(status_code=resp.status, headers=headers)
    if playlist.is_playlist(path, resp.getheader("content-type") or ""):
        return _playlist(resp, conn, headers, o)

    def relay():
        try:
            while chunk := resp.read(CHUNK):
                yield chunk
        except (OSError, http.client.HTTPException):
            return  # the upstream died mid-body: end the stream; the player re-requests
        finally:
            resp.close()
            conn.close()

    return StreamingResponse(relay(), status_code=resp.status, headers=headers)
