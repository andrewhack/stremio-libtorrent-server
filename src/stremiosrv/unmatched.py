"""Requests no route answered: counted, never logged in full.

Until 1.6.7 a path the server does not implement got the web player's index.html through nginx
(200 for a GET, 405 for a POST) and a bare 404 on the API port, and nothing recorded either. That
is how a missing route stayed invisible for three months (issue #3). Now nginx sends every path
that is not a web-player file to /_unmatched, the app counts its own misses on the API port too,
and /stats.json carries the tally as `unmatchedRoutes`.

Only the method and the SHAPE of the path are kept -- `GET /proxy/*`, `HEAD /{ih}/{name}` -- so an
infohash, a file name, a token or a query string never reaches the counter or the log.
"""
from __future__ import annotations

import logging
import re
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from stremiosrv.library import netguard

log = logging.getLogger(__name__)
router = APIRouter()

MAX_SHAPES = 64
_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")
_IH = re.compile(r"[0-9a-fA-F]{40}")
# First path segments kept by name: the route families the stock server registers or a Stremio
# client is known to call (docs/protocol-map.md and the 2026-09-11 client census), plus this
# server's own. Any other first segment -- a token, a release name, a scanner's probe -- counts as
# `{x}`, so nothing a client put in a path can reach /stats.json or the log. A new client route
# shows as `{x}` too: name it with a request log, then add it here.
KNOWN_FIRST_SEGMENTS = frozenset({
    # the stock server (server.js v4.21.1) and what the clients build against it
    "7zip", "casting", "convert", "create", "device-info", "favicon.ico", "ftp", "get-https",
    "heartbeat", "hlsv2", "hwaccel-profiler", "local-addon", "manifest.json", "network-info",
    "nzb", "opensubHash", "probe", "proxy", "rar", "removeAll", "settings", "stats.json",
    "status", "stream", "subtitleSignature", "subtitles.srt", "subtitles.vtt", "subtitlesTracks",
    "tar", "tgz", "thumb.jpg", "tracks", "transcode", "yt", "zip",
    # this server's own
    "_unmatched", "active.json", "cache", "cache.json", "health", "library", "netcheck.json",
    "pins.json", "transcode.json",
})

_lock = threading.Lock()
_counts: dict[str, int] = {}


def shape(method: str, path: str) -> str:
    """What was asked, never what it named: `GET /proxy/*`, `HEAD /{ih}/{name}`, `POST /create`."""
    verb = method.upper() if method.upper() in _METHODS else "OTHER"
    segs = [s for s in (path or "").split("?", 1)[0].split("/") if s]
    if not segs:
        return f"{verb} /"
    if _IH.fullmatch(segs[0]):
        out = "/{ih}"
        if len(segs) > 1:
            second = segs[1]
            out += "/" + ("{idx}" if second.isdecimal() else "-1" if second == "-1" else "{name}")
        if len(segs) > 2:
            out += "/*"
        return f"{verb} {out}"
    out = "/" + (segs[0] if segs[0] in KNOWN_FIRST_SEGMENTS else "{x}")
    if len(segs) > 1:
        out += "/*"
    return f"{verb} {out}"


def record(method: str, path: str) -> None:
    """Count one unanswered request under its shape.

    The first sighting of each shape reaches the container log, once. Past MAX_SHAPES distinct
    shapes the rest count as `other`, so a scanner cannot grow this without bound."""
    key = shape(method, path)
    with _lock:
        if key not in _counts and len(_counts) >= MAX_SHAPES:
            key = "other"
        new = key not in _counts
        _counts[key] = _counts.get(key, 0) + 1
    if new:
        _announce(key)


def snapshot() -> dict[str, int]:
    """For /stats.json: shape -> requests, since the process started."""
    with _lock:
        return dict(sorted(_counts.items()))


def reset() -> None:
    """Test helper -- forget every count."""
    with _lock:
        _counts.clear()


def _announce(key: str) -> None:
    """A new shape reaches `docker logs`. uvicorn surfaces only its own loggers, so this attaches a
    handler of its own, as library/addon.py's `_announce` does."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [routes] %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    log.info("no route for %s -- counted in /stats.json unmatchedRoutes", key)


class CountUnmatched:
    """ASGI wrapper: count what the app itself had no answer for.

    A 404 with no `endpoint` in the scope is a path no route matched -- Starlette adds `endpoint`
    only when a route matches, so a route's own deliberate 404 (the library addon's token guard, an
    unknown transcode job) is not counted. A 405 is counted only when the matched route itself
    lacks the method (FastAPI leaves the route in the scope, partial matches included): a known
    path asked with a method it does not implement, how an unbuilt `POST /settings` shows. A 405 a
    route passes on from elsewhere -- /proxy relaying an upstream's answer -- is that route
    answering. /_unmatched is itself a route and counts its own requests, so this wrapper never
    sees those as misses.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def watch(message) -> None:
            if message.get("type") == "http.response.start":
                status = message.get("status")
                methods = getattr(scope.get("route"), "methods", None) or ()
                if (status == 404 and "endpoint" not in scope) or (
                        status == 405 and scope.get("method") not in methods):
                    record(scope.get("method", ""), scope.get("path", ""))
            await send(message)

        await self.app(scope, receive, watch)


@router.api_route("/_unmatched", methods=list(_METHODS), include_in_schema=False)
def unmatched(request: Request) -> JSONResponse:
    """nginx's answer for every path that is not a web-player file (docker/nginx-locations.inc).

    The request nginx could not place arrives in two headers, believed only from a loopback peer --
    our own nginx, the rule netguard.client_ip already applies to X-Forwarded-For. From anyone else
    this is counted as what it literally is: a request for /_unmatched.
    """
    peer = request.client.host if request.client else ""
    method, uri = request.method, request.url.path
    if netguard._is_loopback(peer):
        method = request.headers.get("x-original-method") or method
        uri = request.headers.get("x-original-uri") or uri
    record(method, uri)
    return JSONResponse({"detail": "Not Found"}, status_code=404)
