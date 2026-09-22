"""ffmpeg's private reader of a playing torrent for subtitle extraction.

The TV plays a torrent via the stream route (refocus, deadlines, watched marking). ffmpeg extracts
embedded subtitles from the same file. Going through the stream route would refocus the torrent and
drop the TV's playhead deadlines, so ffmpeg gets its own reader: byte ranges from the engine's file,
waiting for pieces with every deadline 2 s after the viewer's, uncounted, and never refocusing,
changing the focus or marking the torrent watched.

The reader is loopback-only and requires a per-process secret in the path. nginx has no location for
it, so the allowlist test keeps it that way.
"""
from __future__ import annotations

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

# Per-process secret. Only this process's ffmpeg may read; a peer that knows only the secret
# cannot reach this server because it comes through loopback, must have no X-Forwarded-For,
# and only this running process holds this secret (lost on restart).
_SECRET = secrets.token_urlsafe(32)


def _engine(request: Request):
    return getattr(request.app.state, "engine", None)


def reader_url(request: Request, info_hash: str, idx: int) -> str:
    """The URL ffmpeg uses to read a file the TV is playing.

    Arguments:
        request: the FastAPI request (carries request.app with settings)
        info_hash: the torrent's 40-char hex infohash
        idx: the file index in the torrent

    Returns:
        a URL like http://127.0.0.1:12345/_embedded-ass-read/<secret>/<ih>/<idx>
    """
    http_port = request.app.state.settings.http_port
    return f"http://127.0.0.1:{http_port}{READER_PREFIX}/{_SECRET}/{info_hash}/{idx}"


def _playing(request: Request, info_hash: str, idx: int):
    """The engine's handle for this torrent, if it exists and has metadata; None otherwise."""
    eng = _engine(request)
    if eng is None:
        return None
    h = eng.get(info_hash)
    if h is None or not h.has_metadata():
        return None
    if idx < 0 or idx >= h.num_files():
        return None
    return h


def _is_own_ffmpeg(request: Request) -> bool:
    """Only ffmpeg running on this exact process may read.

    Three guards: loopback peer, per-process secret in the path, and no X-Forwarded-For
    (which would mean the request came through a proxy, not directly from the process).
    """
    peer = request.client.host if request.client else ""
    return (
        netguard._is_loopback(peer)
        and "x-forwarded-for" not in request.headers
    )


@router.get(f"{READER_PREFIX}/{{secret}}/{{info_hash}}/{{idx:int}}")
def read(secret: str, info_hash: str, idx: int, request: Request):
    """Byte-range reader for ffmpeg extracting embedded subtitles from a playing torrent.

    Returns 206 with byte ranges, 416 for out-of-range, 404 for unauthorized or invalid.
    Never refocuses the torrent, changes file focus, or marks it watched.
    """
    # Guard: loopback only, correct secret, no X-Forwarded-For
    if not _is_own_ffmpeg(request) or secret != _SECRET:
        return Response(status_code=404)

    # Get the torrent and validate it exists with metadata
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
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}"},
        )

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
