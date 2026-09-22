"""ffmpeg's private reader for the embedded-ASS routes: who may use it, and what it must not do.

The reader serves a torrent file the TV is already playing. Going through the stream route instead
would `refocus()` the torrent on every request and take the TV's playhead priority away.
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Generator

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from stremiosrv.library import netguard
from stremiosrv.stream.fileserver import content_type_for, wait_and_read
from stremiosrv.stream.ranges import parse_range

router = APIRouter()

READER_PREFIX = "/_embedded-ass-read"
READER_DEADLINE_OFFSET_MS = 2000
READER_FIRST_TIMEOUT = 20.0
READER_TIMEOUT = 10.0

# Per-process secret: unique to this running process, lost on restart.
_SECRET = secrets.token_urlsafe(32)
_INFOHASH = re.compile(r"^[0-9a-f]{40}$")


def _engine(request: Request):
    return getattr(request.app.state, "engine", None)


def reader_url(request: Request, info_hash: str, idx: int) -> str:
    """Build the private reader's URL for ffmpeg to fetch from.

    Arguments:
        request: FastAPI request (carries request.app.state.settings.http_port)
        info_hash: the torrent's 40-char hex infohash
        idx: file index in the torrent

    Returns:
        URL like http://127.0.0.1:11470/_embedded-ass-read/<secret>/<ih>/<idx>
    """
    http_port = request.app.state.settings.http_port
    return f"http://127.0.0.1:{http_port}{READER_PREFIX}/{_SECRET}/{info_hash}/{idx}"


def _playing(request: Request, info_hash: str, idx: int):
    """The engine's handle if this torrent is loaded with metadata; None otherwise."""
    if not _INFOHASH.fullmatch(info_hash):
        return None
    eng = _engine(request)
    if eng is None:
        return None
    h = eng.get(info_hash)
    if h is None or not h.has_metadata():
        return None
    if idx < 0 or idx >= h.num_files():
        return None
    return h


def _is_own_ffmpeg(request: Request, secret: str) -> bool:
    """Is this request from this process's ffmpeg, at loopback, with the right secret?

    The private reader answers only a loopback peer with the per-process secret, and rejects
    any X-Forwarded-For (a sign the request came through a proxy, not from the local process).
    The secret is compared in constant time to prevent timing attacks.
    """
    peer = request.client.host if request.client else ""
    if not netguard._is_loopback(peer) or "x-forwarded-for" in request.headers:
        return False
    return secrets.compare_digest(secret.encode(), _SECRET.encode())


@router.get(READER_PREFIX + "/{secret}/{info_hash}/{idx}", include_in_schema=False, name="private_reader")
def read(secret: str, info_hash: str, idx: int, request: Request):
    """Byte-range reader for ffmpeg extracting embedded subtitles from a playing torrent.

    Returns 206 with byte ranges, 416 for out-of-range, 404 for auth/validation failures.
    Waits for pieces with every deadline 2s after the TV's viewer, and never refocuses,
    changes focus, or marks the torrent watched.
    """
    # Guard: loopback + secret in constant time + no X-Forwarded-For
    if not _is_own_ffmpeg(request, secret):
        return Response(status_code=404)

    # Get torrent, validate it exists and has metadata
    h = _playing(request, info_hash, idx)
    if h is None:
        return Response(status_code=404)

    eng = _engine(request)
    if eng is None:
        return Response(status_code=503, content=b"engine unavailable")

    total = h.file_size(idx)
    start, end = parse_range(request.headers.get("Range"), total)

    # Out-of-range: start >= total means no valid range (RFC 7233 4.4)
    if start >= total:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(end - start + 1),
        "Content-Type": content_type_for(h.file_path(idx)),
    }

    def reader_stream() -> Generator[bytes, None, None]:
        """Serve bytes [start, end] (inclusive) using wait_and_read with reader settings."""
        stream = wait_and_read(
            eng.save_path(), h, idx, start, end,
            timeout=READER_TIMEOUT,
            first_timeout=READER_FIRST_TIMEOUT,
            count=False,
            deadline_offset_ms=READER_DEADLINE_OFFSET_MS,
            info_hash=info_hash,
        )
        try:
            yield from stream
        finally:
            stream.close()

    return StreamingResponse(reader_stream(), status_code=206, headers=headers)
