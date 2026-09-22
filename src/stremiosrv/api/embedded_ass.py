"""Embedded ASS subtitles for TV players: the routes. What they compute is subs/embedded_ass.py.

A TV plays a torrent's file directly. With the "ASS subtitles styling" setting on, stremio-video
0.0.97+ asks which ASS tracks and fonts the file has, then fetches the selected track 60 s at a
time as playback moves, and draws it with libass over the video. Only this server's own torrent
streams are answered: every route here reads a file the engine already holds, and never a URL a
client sent.

ffmpeg reads that file through a private reader, never through the stream route (/{ih}/{idx}).
The stream route calls `refocus()` on every request -- dropping the viewer's playhead deadlines --
and `focus_file()`, marks the torrent watched, and counts stalls. A subtitle reader going through
it every 30 s would take the TV's own playhead priority away.
"""
from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from stremiosrv.library import netguard
from stremiosrv.stream.fileserver import content_type_for, wait_and_read
from stremiosrv.stream.ranges import parse_range

router = APIRouter()

# --- the private reader: ffmpeg's way into a torrent file ---

READER_PREFIX = "/_embedded-ass-read"
# Made per process, never logged and never sent to a client: only an ffmpeg this process starts is
# handed a URL carrying it.
_SECRET = secrets.token_urlsafe(32)
# Later than any deadline the viewer's own reads set, so the TV's playhead pieces come first. The
# reader still asks for its pieces: after a seek its window starts up to 40 s behind the TV's new
# position, where nothing has downloaded.
READER_DEADLINE_OFFSET_MS = 2000
READER_FIRST_TIMEOUT = 20.0
READER_TIMEOUT = 10.0
_INFOHASH = re.compile(r"[0-9a-f]{40}")


def reader_url(request: Request, info_hash: str, idx: int) -> str:
    """The URL ffmpeg reads file `idx` of torrent `info_hash` from."""
    port = request.app.state.settings.http_port
    return f"http://127.0.0.1:{port}{READER_PREFIX}/{_SECRET}/{info_hash}/{idx}"


def _is_own_ffmpeg(request: Request, secret: str) -> bool:
    """Only an ffmpeg this process started: the right secret, from loopback, not through nginx.

    nginx always sets X-Forwarded-For (docker/nginx-locations.inc), so a loopback peer without it
    connected directly; a LAN client on :11470 is not loopback; and the secret makes the caller
    this process's own."""
    peer = request.client.host if request.client else ""
    return (secrets.compare_digest(secret.encode(), _SECRET.encode())
            and netguard._is_loopback(peer) and "x-forwarded-for" not in request.headers)


def _playing(request: Request, info_hash: str, idx: int):
    """The torrent's handle when the engine holds it, with metadata and a file `idx`; else None.

    Nothing here ever adds a torrent: the TV is playing this file, so the stream route has."""
    eng = getattr(request.app.state, "engine", None)
    if eng is None or not _INFOHASH.fullmatch(info_hash):
        return None
    h = eng.get(info_hash)
    if h is None or not h.has_metadata() or not 0 <= idx < h.num_files():
        return None
    return h


@router.get(READER_PREFIX + "/{secret}/{info_hash}/{idx}", include_in_schema=False)
def private_reader(secret: str, info_hash: str, idx: int, request: Request) -> Response:
    """Byte ranges of a torrent file for ffmpeg, waiting for pieces like the stream route does.

    Unlike it: never `refocus()` or `focus_file()`, no stall or timeout counted, the torrent never
    marked watched, and every deadline 2 s later than the viewer's own."""
    h = _playing(request, info_hash, idx) if _is_own_ffmpeg(request, secret) else None
    if h is None:
        return Response(status_code=404)
    total = h.file_size(idx)
    start, end = parse_range(request.headers.get("Range"), total)
    if start >= total:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(end - start + 1),
        "Content-Type": content_type_for(h.file_path(idx)),
    }
    body = wait_and_read(
        request.app.state.engine.save_path(), h, idx, start, end,
        timeout=READER_TIMEOUT, first_timeout=READER_FIRST_TIMEOUT, info_hash=info_hash,
        count=False, deadline_offset_ms=READER_DEADLINE_OFFSET_MS,
    )
    return StreamingResponse(body, status_code=206, headers=headers)
