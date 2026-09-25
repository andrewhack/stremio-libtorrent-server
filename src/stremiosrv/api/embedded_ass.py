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

import asyncio
import functools
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from stremiosrv import metrics
from stremiosrv.api.subs import parse_stream_url
from stremiosrv.library import netguard
from stremiosrv.stream.fileserver import content_type_for, wait_and_read
from stremiosrv.stream.ranges import parse_range
from stremiosrv.subs.embedded_ass import (
    Discovery,
    discover,
    font_dump_argv,
    parse_window,
    probe_argv,
    window_argv,
)

logger = logging.getLogger("stremiosrv.embedded_ass")
router = APIRouter()

# --- the private reader: ffmpeg's way into a torrent file ---

READER_PREFIX = "/_embedded-ass-read"
# Made per process, never logged and never sent to a client: only an ffmpeg this process starts is
# handed a URL carrying it.
_SECRET = secrets.token_urlsafe(32)
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
    marked watched, and it yields to the viewer (wait_and_read's `yield_to_viewer`): it never moves
    a deadline the viewer set, and its own come after the viewer's next 32 MiB. It still asks for
    its pieces: after a seek its window starts up to 40 s behind the TV's new position, where
    nothing has downloaded."""
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
        count=False, yield_to_viewer=True,
    )
    return StreamingResponse(body, status_code=206, headers=headers)


# --- the three routes the TV calls ---

PROBE_TIMEOUT = 30
FFMPEG_TIMEOUT = 30
HEAD_POLL = 0.2  # seconds between looks while discovery waits for a file's first piece
# At most this many extractions and font dumps at once. The client aborts a window it no longer
# needs, but the work runs on a thread that cannot see that, so its ffmpeg runs to the end.
_WORK = threading.BoundedSemaphore(2)
# The routes' blocking work -- a per-file lock, the extraction limit, ffprobe, ffmpeg -- runs on
# threads of its own, never on the server's shared worker pool. A TV loading a styled track asks
# for every font at once; blocked on the shared pool, those requests would starve the private
# reader their own ffmpeg reads through, and the TV's video stream, which uses the same pool.
_TV_THREADS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="embedded-ass")
_WORK_WAIT = 10.0
_FOUND_KEEP = 16   # files whose track list is remembered
_FONTS_KEEP = 8    # files whose fonts stay on disk
# Outside cache_root on purpose: the evictor and the transcode GC never see it.
FONT_ROOT = os.path.join(tempfile.gettempdir(), "stremiosrv-embedded-ass")

_lock = threading.Lock()
_found: OrderedDict[tuple[str, int], Discovery] = OrderedDict()
_file_locks: dict[tuple[str, int], threading.Lock] = {}
_fonts_used: OrderedDict[tuple[str, int], None] = OrderedDict()
_font_root_ready = False


def reset() -> None:
    """Test helper -- forget every cached track list and font set."""
    global _font_root_ready
    with _lock:
        _found.clear()
        _file_locks.clear()
        _fonts_used.clear()
        _font_root_ready = False


def _fail(status: int, detail: str, info_hash: str, idx: int) -> HTTPException:
    """A 5xx: counted, and logged by infohash and file index only -- never a name or a line."""
    metrics.record_embedded_ass("failure")
    logger.warning("embedded ASS: %s [%s file %d]", detail, info_hash, idx)
    return HTTPException(status_code=status, detail=detail)


def _file_lock(key: tuple[str, int]) -> threading.Lock:
    with _lock:
        return _file_locks.setdefault(key, threading.Lock())


async def _off_pool(fn, *args):
    """Run `fn(*args)` on the routes' own threads, and wait for it without holding a thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_TV_THREADS, functools.partial(fn, *args))


def _resolve(request: Request, media_url: str | None) -> tuple[str, int]:
    """The torrent and file a client's mediaURL names -- when it is this server's own stream of a
    torrent the engine holds. The URL itself is never fetched."""
    parsed = parse_stream_url(media_url or "")
    if parsed is None or _playing(request, *parsed) is None:
        raise HTTPException(status_code=404, detail="not a torrent stream this server is playing")
    return parsed


def _wait_for_head(request: Request, info_hash: str, idx: int) -> None:
    """Wait for the file's first piece before ffprobe reads it.

    The TV asks for discovery the moment it starts playing -- often before the torrent has
    delivered a byte -- and asks only once. ffprobe reads through the private reader, whose waits
    are short, so on a cold torrent it would find nothing and the TV would keep plain subtitles for
    the whole playback. So wait here, on the routes' own threads, as long as the TV's own first read
    may (stream_first_piece_timeout). Nothing is boosted: the TV's own read asks for that piece."""
    h = _playing(request, info_hash, idx)
    if h is None:  # the engine dropped it since the request was resolved
        raise HTTPException(status_code=404, detail="not a torrent stream this server is playing")
    first = h.file_offset(idx) // h.piece_length()
    give_up = time.monotonic() + request.app.state.settings.stream_first_piece_timeout
    while True:
        try:
            if h.have_piece(first):
                return
        except Exception as e:  # a torrent removed mid-wait: its libtorrent handle raises
            raise _fail(502, "the torrent was removed while waiting", info_hash, idx) from e
        if time.monotonic() > give_up:
            raise _fail(504, "the file's first piece did not arrive", info_hash, idx)
        time.sleep(HEAD_POLL)


def _discovery(request: Request, info_hash: str, idx: int) -> Discovery:
    """The file's tracks and fonts, probed once and remembered for the last _FOUND_KEEP files."""
    key = (info_hash, idx)
    with _lock:  # a remembered file never waits on its file lock, which a font dump may hold
        if key in _found:
            _found.move_to_end(key)
            return _found[key]
    with _file_lock(key):
        with _lock:
            if key in _found:
                _found.move_to_end(key)
                return _found[key]
        _wait_for_head(request, info_hash, idx)
        try:
            proc = subprocess.run(probe_argv(reader_url(request, info_hash, idx)),
                                  capture_output=True, timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise _fail(504, "probe timed out", info_hash, idx) from e
        try:
            if proc.returncode != 0:
                raise ValueError(proc.returncode)
            found = discover(json.loads(proc.stdout or b"{}"))
        except ValueError as e:
            raise _fail(502, "probe failed", info_hash, idx) from e
        with _lock:
            _found[key] = found
            while len(_found) > _FOUND_KEEP:
                _found.popitem(last=False)
        return found


def _run(argv: list[str], info_hash: str, idx: int) -> bytes:
    """One ffmpeg run, under the extraction limit."""
    if not _WORK.acquire(timeout=_WORK_WAIT):
        raise _fail(503, "subtitle extraction busy", info_hash, idx)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise _fail(504, "subtitle extraction timed out", info_hash, idx) from e
    finally:
        _WORK.release()
    if proc.returncode != 0:
        raise _fail(502, "subtitle extraction failed", info_hash, idx)
    return proc.stdout


@router.get("/embedded-ass")
async def embedded_ass(request: Request, mediaURL: str | None = None) -> dict:
    """`{"tracks": [{number, codec, lang, label}], "fonts": [{id}]}` for the file being played."""
    return await _off_pool(_embedded_ass, request, mediaURL)


def _embedded_ass(request: Request, media_url: str | None) -> dict:
    info_hash, idx = _resolve(request, media_url)
    found = _discovery(request, info_hash, idx)
    metrics.record_embedded_ass("ask")
    return found.answer()


@router.get("/embedded-ass/{number:int}.ass")
async def embedded_ass_window(
    number: int, request: Request, mediaURL: str | None = None,
    from_: str | None = Query(None, alias="from"), to: str | None = None,
) -> Response:
    """One window of an ASS track: its header and styles, then its events at the file's own
    times. The client asks for `from`..`to` in milliseconds, 60 s at a time."""
    return await _off_pool(_window, number, request, mediaURL, from_, to)


def _window(number: int, request: Request, media_url: str | None, from_q: str | None,
            to_q: str | None) -> Response:
    window = parse_window(from_q, to_q)
    if window is None:
        raise HTTPException(status_code=400,
                            detail="from and to are whole milliseconds, 0 < to - from <= 300000")
    info_hash, idx = _resolve(request, media_url)
    if not _discovery(request, info_hash, idx).has_track(number):
        raise HTTPException(status_code=404, detail="no such ASS track")
    text = _run(window_argv(reader_url(request, info_hash, idx), number, *window), info_hash, idx)
    metrics.record_embedded_ass("window")
    return Response(content=text, media_type="text/x-ssa; charset=utf-8")


def _font_root() -> str:
    """The font cache, emptied the first time this process uses it: what a previous process left
    behind is not trusted."""
    global _font_root_ready
    with _lock:
        if not _font_root_ready:
            shutil.rmtree(FONT_ROOT, ignore_errors=True)
            os.makedirs(FONT_ROOT, exist_ok=True)
            _font_root_ready = True
    return FONT_ROOT


def _fonts_dir(request: Request, info_hash: str, idx: int, found: Discovery) -> str:
    """The directory holding every font of the file, dumped in one ffmpeg run on first use. Only
    the last _FONTS_KEEP files' fonts are kept."""
    key = (info_hash, idx)
    d = os.path.join(_font_root(), f"{info_hash}-{idx}")
    done = os.path.join(d, ".done")
    with _file_lock(key):
        if not os.path.exists(done):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d)
            _run(font_dump_argv(reader_url(request, info_hash, idx), sorted(found.fonts), d),
                 info_hash, idx)
            if not all(os.path.exists(os.path.join(d, f"{i}.font")) for i in found.fonts):
                raise _fail(502, "font dump incomplete", info_hash, idx)
            open(done, "wb").close()
    with _lock:
        _fonts_used[key] = None
        _fonts_used.move_to_end(key)
        stale = []
        while len(_fonts_used) > _FONTS_KEEP:
            stale.append(_fonts_used.popitem(last=False)[0])
    for ih, i in stale:
        shutil.rmtree(os.path.join(FONT_ROOT, f"{ih}-{i}"), ignore_errors=True)
    return d


@router.get("/embedded-ass/font/{font_id:int}")
async def embedded_ass_font(
    font_id: int, request: Request, mediaURL: str | None = None,
) -> Response:
    """One font attachment's bytes."""
    return await _off_pool(_font, font_id, request, mediaURL)


def _font(font_id: int, request: Request, media_url: str | None) -> Response:
    info_hash, idx = _resolve(request, media_url)
    found = _discovery(request, info_hash, idx)
    if font_id not in found.fonts:
        raise HTTPException(status_code=404, detail="no such font")
    d = _fonts_dir(request, info_hash, idx, found)
    return FileResponse(os.path.join(d, f"{font_id}.font"), media_type=found.fonts[font_id])
