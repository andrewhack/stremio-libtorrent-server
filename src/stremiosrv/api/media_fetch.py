"""ffmpeg's guarded way to read an EXTERNAL media URL.

The transcode routes (/hlsv2, /{ih}/{idx}/subtitles.*) get a `mediaURL` from the client. When it is
one of our own URLs -- a torrent stream /{ih}/{idx}, an already-proxied /proxy/... URL, or loopback
-- ffmpeg may read it directly. When it is an external http(s) URL (a plain debrid/HTTP stream), the
server must not let ffmpeg fetch it: ffmpeg 4.4.1 follows redirects itself, so a public URL that
302s into the LAN would slip past a one-shot check. Instead the URL is registered as a ticket and
ffmpeg is handed a loopback reader URL (Task 6's route); the reader fetches through the same
destination guard /proxy uses, re-checking every redirect, with the home-vs-internet decision taken
from the ORIGINAL client request -- never from ffmpeg's own loopback call.
"""
from __future__ import annotations

import secrets
import urllib.parse
from collections import OrderedDict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request

from stremiosrv.library import netguard
from stremiosrv.proxy import client, dest

router = APIRouter()

READER_PREFIX = "/_hls-media-read"
# Per process, never logged, never sent to a client: only an ffmpeg this process starts is handed a
# URL carrying it. The reader also requires a valid ticket, so the secret is one of two factors.
_SECRET = secrets.token_urlsafe(32)

# Recently-registered external fetches: ticket id -> (url, home). Bounded, most-recently-used kept
# (an active transcode reads its input repeatedly, refreshing recency, so a live ticket is never the
# oldest; a finished probe's ticket falls out). No explicit lifecycle, so no coupling to Converter.
TICKET_CAP = 64
_tickets: OrderedDict[str, tuple[str, bool]] = OrderedDict()
_tickets_lock = Lock()


def reset() -> None:
    """Drop every ticket (tests)."""
    with _tickets_lock:
        _tickets.clear()


def register(url: str, home: bool) -> str:
    """Record an external fetch and return its unguessable ticket id."""
    ticket = secrets.token_urlsafe(16)
    with _tickets_lock:
        _tickets[ticket] = (url, home)
        _tickets.move_to_end(ticket)
        while len(_tickets) > TICKET_CAP:
            _tickets.popitem(last=False)
    return ticket


def resolve_ticket(ticket: str) -> tuple[str, bool] | None:
    """The (url, home) a ticket names, refreshing its recency; None if unknown."""
    with _tickets_lock:
        hit = _tickets.get(ticket)
        if hit is not None:
            _tickets.move_to_end(ticket)
        return hit


def reader_url(request: Request, ticket: str) -> str:
    """The loopback URL ffmpeg reads a ticket's fetch from."""
    port = request.app.state.settings.http_port
    return f"http://127.0.0.1:{port}{READER_PREFIX}/{_SECRET}/{ticket}"


def _is_own_ffmpeg(request: Request, secret: str) -> bool:
    """Only an ffmpeg this process started: the right secret, from loopback, not through nginx."""
    peer = request.client.host if request.client else ""
    return (secrets.compare_digest(secret.encode(), _SECRET.encode())
            and netguard._is_loopback(peer) and "x-forwarded-for" not in request.headers)


def is_own_media_url(request: Request, media_url: str) -> bool:
    """Whether `media_url` points at THIS server -- so ffmpeg may read it directly.

    Our own when its host is a loopback address, the host the request was sent to, or the host
    SERVER_URL names. Judged on the real hostname (urlsplit drops any userinfo), at any port and
    either scheme. Everything else is external and must go through the reader."""
    try:
        host = urllib.parse.urlsplit(media_url).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower()
    settings = request.app.state.settings
    own = {client.host_of("//" + request.headers.get("host", "")), client.host_of(settings.server_url)}
    return netguard._is_loopback(host) or host in own


def resolve_media_input(request: Request, media_url: str) -> str:
    """The URL ffmpeg should actually open for this `mediaURL`.

    Our own URL: returned unchanged (ffmpeg reads it directly). An external http(s) URL: dest-checked
    for the original client, then returned as a loopback reader URL. Anything else: 403."""
    if is_own_media_url(request, media_url):
        return media_url
    u = urllib.parse.urlsplit(media_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HTTPException(status_code=403, detail="media source not allowed")
    home = client.is_home_client(request)
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        dest.pick(u.hostname, port, home)  # early, clean refusal; the reader re-checks every hop
    except dest.Refused as e:
        raise HTTPException(status_code=403, detail="media source not allowed") from e
    except OSError as e:  # name does not resolve
        raise HTTPException(status_code=502, detail="media source unreachable") from e
    return reader_url(request, register(media_url, home))
