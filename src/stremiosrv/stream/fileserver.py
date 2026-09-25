"""Serve byte ranges of a torrent file, waiting for the covering pieces to download.

Disk-read strategy: libtorrent writes pieces into `save_path/<file_path>`; once a piece is
present (`have_piece`) we read that region straight off disk. Pieces over the requested range
are raised to top priority by the caller (sequential "head & holes").
"""
from __future__ import annotations

import logging
import mimetypes
import os
import time
from collections.abc import Generator

from stremiosrv import metrics

logger = logging.getLogger("stremiosrv.stream")

# Browser <video> needs a recognized media type or it refuses the source ("video not supported").
# mimetypes doesn't know some container extensions (e.g. .mkv), so map the common ones explicitly.
VIDEO_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".mov": "video/quicktime",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".ogv": "video/ogg",
    ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
}


def content_type_for(path: str) -> str:
    """Best-effort media type from a file's extension (for the Content-Type stream header)."""
    ext = os.path.splitext(path)[1].lower()
    return VIDEO_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def is_video(name: str) -> bool:
    """Whether a file is a video, by its name: one of the types this server streams as one.

    The library's one answer, for its listing, for matching an episode and for a title's card page.
    The listing used to ask the download path's shorter list (pins.VIDEO_EXT), so a .wmv, a .mpg or
    a .flv streamed here yet was never listed -- never learned at its first play, never offered.
    """
    return os.path.splitext(name or "")[1].lower() in VIDEO_TYPES


def file_disk_path(save_path: str, handle, idx: int) -> str:
    return os.path.join(save_path, handle.file_path(idx))


# How much of the viewer's own read-ahead always comes before anything a second reader asks for
# (see wait_and_read's `yield_to_viewer`). Bytes, not pieces, so it means the same on every torrent.
VIEWER_FIRST_BYTES = 32 * 2**20


def wait_and_read(
    save_path: str, handle, idx: int, start: int, end: int,
    timeout: float = 30.0, first_timeout: float = 120.0,
    chunk: int = 262144, window_bytes: int = 50_331_648, step_ms: int = 50,
    info_hash: str = "", count: bool = True, yield_to_viewer: bool = False,
) -> Generator[bytes, None, None]:
    """Yield bytes [start, end] (inclusive, file-relative) of file `idx`, blocking per chunk
    until the covering piece is available.

    Cold-start resilience: a peer-starved box (no inbound port-forward) downloads the playhead
    pieces slowly, so the FIRST piece gets a longer budget (`first_timeout`) than subsequent ones
    (`timeout`). If a piece still never arrives, the generator **ends the stream cleanly** (logs a
    warning, returns) rather than raising, and the player retries (which succeeds once more of the
    file is cached). Playback therefore works even without port-forwarding, just slower on first play.

    Returning early does NOT by itself keep the log clean: the route has already announced
    `Content-Length: end-start+1`, so a short body makes uvicorn raise "Response content shorter
    than Content-Length" after the fact. Truncating the connection is the correct HTTP signal and is
    kept; `SuppressClientDisconnect` (app.py) is what stops it reaching the error log.

    Maintains a sliding window of boosted+deadlined pieces ahead of the read position. The window
    is a fixed *byte budget* (not a piece count) so on big torrents with large pieces it stays a
    tight ~50 MiB region — a seek rushes the first piece at the target instead of spreading
    bandwidth over ~1 GB.

    `info_hash` is only ever logged. It is passed in rather than read back off the handle for the
    same reason the read cursor is recorded by the caller: this generator must never raise into the
    ASGI layer, and `handle.status().info_hashes.v1` is one more call that could — on a handle the
    evictor has just removed, precisely when the stream is failing and the log matters most.

    `count` and `yield_to_viewer` are for a second reader of a file someone is already watching:
    the embedded-ASS extractor (api/embedded_ass.py), whose ffmpeg reads the TV's file while the TV
    plays it. Its waits are not playback stalls, so `count=False` keeps them out of the stall and
    timeout counters. `yield_to_viewer=True` keeps the viewer first in two ways. It never touches a
    piece that is already boosted: libtorrent keeps one deadline per piece and the last call wins,
    so re-deadlining would push the viewer's playhead pieces behind the reader's. And every deadline
    it does set comes after the viewer's next VIEWER_FIRST_BYTES, whatever the piece size."""
    pos = start  # bound before the try so the except handler can always report where it stopped
    try:
        plen = handle.piece_length()
        offset_ms = max(1, VIEWER_FIRST_BYTES // plen) * step_ms if yield_to_viewer else 0
        base = handle.file_offset(idx)
        path = file_disk_path(save_path, handle, idx)
        total = handle.num_pieces()
        window = max(4, min(total, window_bytes // plen))  # pieces, derived from the byte budget
        deadlined_to = (base + start) // plen - 1  # last piece we've already boosted
        yielded = False  # the first piece (cold start) gets the longer first_timeout budget
        while pos <= end:
            gp = (base + pos) // plen  # global piece index for the current byte position
            # Slide the boost window forward so upcoming pieces are rushed in order.
            far = min(gp + window, total - 1)
            while deadlined_to < far:
                deadlined_to += 1
                delay = offset_ms + max(0, deadlined_to - gp) * step_ms
                if yield_to_viewer:
                    handle.boost_piece(deadlined_to, delay, keep_existing=True)
                else:  # the viewer's own boosts always win, so they replace whatever is there
                    handle.boost_piece(deadlined_to, delay)
            budget = timeout if yielded else first_timeout
            deadline = time.time() + budget
            wait_start = time.time()
            had_to_wait = not handle.have_piece(gp)  # piece not ready = playback waits for data
            while not handle.have_piece(gp) and time.time() < deadline:
                time.sleep(0.2)
            if not handle.have_piece(gp):
                # Give up gracefully: end the stream (no raise) so the player just retries. Common on
                # peer-starved boxes — surfaced via /netcheck. The resulting short body is absorbed
                # by SuppressClientDisconnect; this warning is the diagnostic that survives.
                # `served` is the field to read first. 0 means the stall was AT the requested offset
                # — a cold seek target — so the swarm never delivered the piece the player asked for.
                # Non-zero means the window ran dry partway through and the budget that expired was
                # the shorter `timeout`, not `first_timeout`.
                if count:
                    metrics.record_timeout()
                logger.warning(
                    "piece %d/%d not available within %.0fs (peer-starved?); ending stream "
                    "[%s file %d, byte %d of range %d-%d, %d served]",
                    gp, total, budget, info_hash or "?", idx, pos, start, end, pos - start,
                )
                return
            if had_to_wait and count:
                metrics.record_stall(time.time() - wait_start)
            # Never read past the end of the current (verified) piece: the next piece may not be
            # downloaded yet, and reading into it would return sparse/zero bytes -> corrupt frames.
            piece_last = (gp + 1) * plen - 1 - base  # last file-relative byte still in piece gp
            n = min(chunk, end - pos + 1, piece_last - pos + 1)
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read(n)
            if not data:
                break
            yield data
            yielded = True
            pos += len(data)
    except Exception as e:  # noqa: BLE001 — must NEVER bubble into the ASGI layer
        # Any mid-stream error — file not on disk yet (FileNotFoundError), the torrent handle removed
        # by the evictor mid-stream (invalid-handle), or a transient disk I/O error — ends the stream
        # cleanly rather than propagating; the player simply re-requests the range and succeeds once
        # the data is present. As above, the resulting short body is absorbed by
        # SuppressClientDisconnect. (Client disconnects raise GeneratorExit, not Exception, so they
        # pass through and close the generator normally.)
        if count:
            metrics.record_timeout()
        logger.warning(
            "stream ended early (%s: %s); player will retry "
            "[%s file %d, byte %d of range %d-%d, %d served]",
            type(e).__name__, e, info_hash or "?", idx, pos, start, end, pos - start,
        )
        return
