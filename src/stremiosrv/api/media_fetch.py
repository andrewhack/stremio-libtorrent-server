"""ffmpeg's guarded way to read an EXTERNAL media URL.

The transcode routes (/hlsv2, /{ih}/{idx}/subtitles.*) get a `mediaURL` from the client. When it is
our own torrent-stream URL -- /{ih}/{idx}, at our loopback address or the SERVER_URL host -- ffmpeg
may read it directly. Everything else must NOT be handed to ffmpeg directly, including our own
`/proxy/...` URL: ffmpeg 4.4.1 follows redirects itself, so a public URL that 302s into the LAN
would slip past a one-shot check, and a /proxy URL would have ffmpeg re-enter /proxy over loopback,
where the destination guard sees only the loopback caller and classifies it HOME -- an internet
client's own mediaURL escalating to a home-classified fetch (C1). Instead the URL -- or, for a
/proxy URL, its INNER destination, parsed out with the same opts.parse /proxy itself uses -- is
registered as a ticket and ffmpeg is handed a loopback reader URL (Task 6's route); the reader
fetches through the same destination guard /proxy uses, re-checking every redirect, with the
home-vs-internet decision taken from the ORIGINAL client request -- never from ffmpeg's own loopback
call.
"""
from __future__ import annotations

import http.client
import logging
import re
import secrets
import urllib.parse
import weakref
from collections import OrderedDict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from stremiosrv.library import netguard
from stremiosrv.proxy import client, dest, opts, playlist, upstream

router = APIRouter()
logger = logging.getLogger(__name__)

READER_PREFIX = "/_hls-media-read"
# Per process, never logged, never sent to a client: only an ffmpeg this process starts is handed a
# URL carrying it. The reader also requires a valid ticket, so the secret is one of two factors.
_SECRET = secrets.token_urlsafe(32)

# Recently-registered external fetches: ticket id -> (url, home, headers). Bounded, most-recently-
# used kept (an active transcode reads its input repeatedly, refreshing recency, so a live ticket is
# never the oldest; a finished probe's ticket falls out). No explicit lifecycle, so no coupling to
# Converter. `headers` carries a proxied mediaURL's own `h=` request headers -- empty for a plain
# external fetch.
#
# The cap must exceed the segment count of one VOD playlist: rewriting a playlist registers a ticket
# for EVERY segment in a single burst, before ffmpeg fetches the first, so LRU-by-recency cannot
# protect a not-yet-fetched early segment -- too small a cap evicts the earliest segments before
# ffmpeg asks for them and it gets a 404 (the bug this size closes). A few thousand covers a
# multi-hour VOD (8192 ~ 4 h of 2 s segments); a live playlist re-fetched in windows only ever
# evicts its oldest, already-consumed segments, which is correct. A playlist still longer than this
# logs a loud warning (see _playlist_response) rather than dropping segments silently.
TICKET_CAP = 8192
_Ticket = tuple[str, bool, tuple[tuple[str, str], ...]]
_tickets: OrderedDict[str, _Ticket] = OrderedDict()
_tickets_lock = Lock()


def reset() -> None:
    """Drop every ticket (tests)."""
    with _tickets_lock:
        _tickets.clear()


def register(url: str, home: bool, headers: tuple[tuple[str, str], ...] | None = None) -> str:
    """Record an external fetch and return its unguessable ticket id."""
    ticket = secrets.token_urlsafe(16)
    with _tickets_lock:
        _tickets[ticket] = (url, home, tuple(headers or ()))
        _tickets.move_to_end(ticket)
        while len(_tickets) > TICKET_CAP:
            _tickets.popitem(last=False)
    return ticket


def resolve_ticket(ticket: str) -> _Ticket | None:
    """The (url, home, headers) a ticket names, refreshing its recency; None if unknown."""
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


# The inert torrent-stream shape -- exactly /<40-hex-infohash>/<idx> (idx may be signed, e.g. -1 for
# core's default), nothing more. The ONLY shape ffmpeg may ever be handed directly. No trailing
# subpath: a legitimate own stream URL never carries one (playback.py's stream routes are exactly
# /{info_hash}/{idx:int} and /{info_hash}/-1), and allowing one here previously let an own-host
# mediaURL like /<ih>/<idx>/subtitles.json?mediaURL=<LAN> pass through unchanged too -- subs.py's
# subtitles routes take their own `mediaURL` and re-invoke resolve_media_input, so ffprobe/ffmpeg
# opening that URL over loopback made the INNER re-invocation see a loopback (home) request,
# regardless of the original client (residual C1, re-review). Anchored at both ends so `.search`
# could never widen it back out even by accident.
_TORRENT_STREAM_PATH = re.compile(r"^/[0-9a-fA-F]{40}/-?\d+$")


def _is_own_host(request: Request, host: str) -> bool:
    """Whether `host` (already lowercased) is a loopback address or the host SERVER_URL names --
    both signals the caller cannot forge."""
    settings = request.app.state.settings
    return netguard._is_loopback(host) or host == client.host_of(settings.server_url)


def is_own_media_url(request: Request, media_url: str) -> bool:
    """Whether `media_url` is our own inert torrent-stream URL -- so ffmpeg may read it directly.

    Our own when its host is a loopback address, or the host SERVER_URL names -- both signals the
    caller cannot forge -- AND its path is the torrent-stream shape (see _TORRENT_STREAM_PATH).
    Judged on the real hostname (urlsplit drops any userinfo), at any port and either scheme.

    Deliberately narrower than "any own-host URL": an own-host `/proxy/...` URL (or any other own
    path) is NOT "own" here, so it never passes through unchanged. Handing ffmpeg a /proxy URL would
    have it re-enter /proxy over loopback, where the destination guard sees only the loopback caller
    and classifies it HOME -- an internet client's own mediaURL escalating to a home-classified
    fetch (C1). `resolve_media_input` routes an own-host /proxy URL through the reader by its INNER
    destination instead, still dest-checked for the original client.

    The request's own Host header is deliberately NOT consulted: nginx forwards it verbatim from
    the client (no server_name / TrustedHostMiddleware), so an internet caller could set Host to
    match an external mediaURL and have this return True -- skipping the guard entirely. A
    legitimate mediaURL is built either against SERVER_URL (stays "own") or, from a LAN page's own
    origin, against that origin -- which then simply counts as external and goes through the
    reader, still dest-checked for the original client, so nothing legitimate breaks."""
    try:
        u = urllib.parse.urlsplit(media_url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if not host:
        return False
    return _is_own_host(request, host) and bool(_TORRENT_STREAM_PATH.match(u.path))


def _resolve_own_proxy_url(request: Request, u: urllib.parse.SplitResult) -> str:
    """The reader URL for an own-host `/proxy/...` mediaURL, routed by its INNER destination (C1).

    ffmpeg must never open a /proxy URL directly (see is_own_media_url's docstring). Instead the
    inner destination is parsed out with the same opts.parse /proxy itself uses -- from `u.path`,
    which urlsplit leaves percent-encoded, exactly what opts.parse expects, the same way
    api/proxy.py reads it from raw_path -- and dest-checked for the ORIGINAL client. A
    proxied/debrid transcode still works (the inner CDN is public, so dest.pick allows it for
    either a home or an internet client); an internet client's inner-LAN target is refused exactly
    as a direct /proxy call would refuse it. The `r=` response headers are ignored: those are for
    the browser, not ffmpeg."""
    parsed = opts.parse(u.path[len("/proxy/"):])
    if parsed is None:
        raise HTTPException(status_code=403, detail="media source not allowed")
    o, inner_path = parsed
    inner_url = o.dest + inner_path + (f"?{u.query}" if u.query else "")
    home = client.is_home_client(request)
    iu = urllib.parse.urlsplit(inner_url)
    port = iu.port or (443 if iu.scheme == "https" else 80)
    try:
        dest.pick(iu.hostname, port, home)  # early, clean refusal; the reader re-checks every hop
    except dest.Refused as e:
        logger.warning("media source refused by destination guard: %s", e)
        raise HTTPException(status_code=403, detail="media source not allowed") from e
    except OSError as e:  # name does not resolve
        raise HTTPException(status_code=502, detail="media source unreachable") from e
    return reader_url(request, register(inner_url, home, o.req_headers))


def resolve_media_input(request: Request, media_url: str) -> str:
    """The URL ffmpeg should actually open for this `mediaURL`.

    Our own torrent-stream URL: returned unchanged (ffmpeg reads it directly). Our own `/proxy/...`
    URL: never handed to ffmpeg -- routed through the reader by its INNER destination instead (C1;
    see _resolve_own_proxy_url). Any other external http(s) URL: dest-checked directly for the
    original client, then returned as a loopback reader URL. Anything else: 403."""
    if is_own_media_url(request, media_url):
        return media_url
    try:
        u = urllib.parse.urlsplit(media_url)
    except ValueError as e:  # malformed (e.g. an unbalanced IPv6 literal): reject, don't 500
        raise HTTPException(status_code=403, detail="media source not allowed") from e
    host = (u.hostname or "").lower()
    if host and _is_own_host(request, host) and u.path.startswith("/proxy/"):
        return _resolve_own_proxy_url(request, u)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HTTPException(status_code=403, detail="media source not allowed")
    home = client.is_home_client(request)
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        dest.pick(u.hostname, port, home)  # early, clean refusal; the reader re-checks every hop
    except dest.Refused as e:
        logger.warning("media source refused by destination guard: %s", e)
        raise HTTPException(status_code=403, detail="media source not allowed") from e
    except OSError as e:  # name does not resolve
        raise HTTPException(status_code=502, detail="media source unreachable") from e
    return reader_url(request, register(media_url, home))


_MAX_PLAYLIST_BYTES = 4 << 20
_CHUNK = 64 << 10


def _segment_mapper(request: Request, base: str, home: bool,
                    headers: tuple[tuple[str, str], ...], counter: list[int]):
    """Map each URL in a fetched playlist to a fresh reader ticket, so ffmpeg fetches its segments
    and keys through the guard too. Relatives are absolutised against the playlist's real URL, not
    the reader URL, because the reader URL carries no path to resolve against.

    A proxied playlist's own request headers (its `h=` options, e.g. an Authorization token) ride
    onto the tickets of segments on the SAME ORIGIN (scheme + host + port) as the playlist, so an
    authenticated HLS/debrid stream does not 401 per segment. They are deliberately NOT attached to
    a segment on a different origin -- a different host (a third-party CDN), a plain-HTTP downgrade
    of an HTTPS playlist, or a different port would each hand the token somewhere the client never
    authenticated. This governs the header we ATTACH here; open_url in turn drops credential headers
    across a cross-origin *redirect* (see upstream.py), so neither path hands a token to an origin
    the client never authenticated to. `counter[0]` counts registered segments so the caller can
    warn past TICKET_CAP."""
    def _origin(u: urllib.parse.SplitResult) -> tuple[str, str, int]:
        return (u.scheme, (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80))

    base_origin = _origin(urllib.parse.urlsplit(base))

    def mapper(url: str) -> str:
        if url.startswith("#") or not url.strip():
            return url
        absolute = urllib.parse.urljoin(base, url)
        u = urllib.parse.urlsplit(absolute)
        if u.scheme not in ("http", "https") or not u.hostname:
            return url  # not fetchable by us; leave it (ffmpeg's protocol whitelist blocks non-http)
        counter[0] += 1
        seg_headers = headers if _origin(u) == base_origin else ()
        return reader_url(request, register(absolute, home, seg_headers))
    return mapper


def _close(resp: http.client.HTTPResponse, conn: http.client.HTTPConnection) -> None:
    """Close both ends of an upstream fetch. Safe to call twice -- http.client's close() is a no-op
    once already closed -- so this may run as a backstop after the streamed body's own finally."""
    resp.close()
    conn.close()


def _playlist_response(resp: http.client.HTTPResponse, raw: bytes, request: Request, url: str,
                       home: bool, headers: tuple[tuple[str, str], ...]) -> Response:
    """The answer for an already-read playlist body: too large, unrewritable, or rewritten. `headers`
    are the fetch's own request headers, carried onto same-host segment tickets (see
    _segment_mapper)."""
    if len(raw) > _MAX_PLAYLIST_BYTES:
        return Response(status_code=502, content=b"playlist too large")
    segments = [0]
    mapper = _segment_mapper(request, url, home, headers, segments)
    try:
        rewritten = playlist.rewrite_lines(raw, mapper, limit=_MAX_PLAYLIST_BYTES)
    except playlist.TooLarge:
        return Response(status_code=502, content=b"playlist too large")
    if segments[0] > TICKET_CAP:
        logger.warning("playlist has %d segments, over the reader ticket cap of %d; its earliest "
                       "segments may be evicted before ffmpeg fetches them", segments[0], TICKET_CAP)
    return Response(content=rewritten, status_code=resp.status,
                    media_type="application/vnd.apple.mpegurl")


@router.get(READER_PREFIX + "/{secret}/{ticket}", include_in_schema=False)
def media_reader(secret: str, ticket: str, request: Request) -> Response:
    """Fetch a ticket's external URL through the destination guard and hand the body to ffmpeg.

    Only our own ffmpeg reaches this (secret + loopback + no X-Forwarded-For). The fetch re-checks
    the destination on every redirect (upstream.open_url) with the ticket's original-client home
    flag. A playlist body is rewritten so its segments and keys come back through the reader too;
    any other body is streamed through as it arrives."""
    hit = resolve_ticket(ticket) if _is_own_ffmpeg(request, secret) else None
    if hit is None:
        return Response(status_code=404)
    url, home, ticket_headers = hit
    fwd = {"user-agent": "Mozilla/5.0"}
    for name, value in ticket_headers:  # a proxied mediaURL's own `h=` headers (C1), e.g. auth
        fwd[name] = value
    rng = request.headers.get("range")
    if rng:
        fwd["range"] = rng
    deadline = upstream.Deadline(upstream.DEADLINE)
    try:
        resp, conn = upstream.open_url(url, "GET", fwd, home, deadline)
    except dest.Refused as e:  # a hop resolved into the LAN/metadata: refused, and worth a log line
        logger.warning("media source refused by destination guard: %s", e)
        return Response(status_code=502, content=b"media source unreachable")
    except Exception:  # noqa: BLE001 — unreachable/malformed/timeout: one clean 502
        return Response(status_code=502, content=b"media source unreachable")
    ctype = resp.getheader("Content-Type", "") or ""
    if playlist.is_playlist(urllib.parse.urlsplit(url).path, ctype):
        try:
            raw = resp.read(_MAX_PLAYLIST_BYTES + 1)
        except (OSError, http.client.HTTPException):
            answer = Response(status_code=502, content=b"media source unreachable")
        else:
            answer = _playlist_response(resp, raw, request, url, home, ticket_headers)
        finally:
            _close(resp, conn)
        # A playlist the deadline cut short (a stalled upstream, most likely) is never served --
        # matches api/proxy.py's _proxied(), which gates its own playlist answer the same way.
        if not deadline.stop():
            return Response(status_code=504, content=b"media source too slow")
        return answer

    def body():
        try:
            while chunk := resp.read1(_CHUNK):
                yield chunk
        except (OSError, http.client.HTTPException):
            return  # the upstream died mid-body: end the stream; the player re-requests
        finally:
            _close(resp, conn)

    headers = {}
    for name in ("content-type", "content-length", "content-range", "accept-ranges"):
        value = resp.getheader(name)
        if value is not None:
            headers[name] = value
    deadline.stop()
    streamed = body()
    # Backstop for the one case the generator's own finally cannot cover: Starlette never starting
    # to iterate it at all (e.g. the client is already gone). Same pattern as api/proxy.py's
    # weakref.finalize(body, _abandon, resp, conn, slot) -- and _close() tolerates running twice,
    # so this racing with the generator's own finally is harmless either way.
    weakref.finalize(streamed, _close, resp, conn)
    return StreamingResponse(streamed, status_code=resp.status, headers=headers)
