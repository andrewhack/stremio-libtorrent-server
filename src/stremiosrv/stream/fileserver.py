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
from collections.abc import Iterator

from stremiosrv import metrics

logger = logging.getLogger("stremiosrv.stream")

# Browser <video> needs a recognized media type or it refuses the source ("video not supported").
# mimetypes doesn't know some container extensions (e.g. .mkv), so map the common ones explicitly.
_VIDEO_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".mov": "video/quicktime",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".ogv": "video/ogg",
    ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
}


def content_type_for(path: str) -> str:
    """Best-effort media type from a file's extension (for the Content-Type stream header)."""
    ext = os.path.splitext(path)[1].lower()
    return _VIDEO_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def file_disk_path(save_path: str, handle, idx: int) -> str:
    return os.path.join(save_path, handle.file_path(idx))


def wait_and_read(
    save_path: str, handle, idx: int, start: int, end: int,
    timeout: float = 30.0, first_timeout: float = 120.0,
    chunk: int = 262144, window_bytes: int = 50_331_648, step_ms: int = 50,
) -> Iterator[bytes]:
    """Yield bytes [start, end] (inclusive, file-relative) of file `idx`, blocking per chunk
    until the covering piece is available.

    Cold-start resilience: a peer-starved box (no inbound port-forward) downloads the playhead
    pieces slowly, so the FIRST piece gets a longer budget (`first_timeout`) than subsequent ones
    (`timeout`). If a piece still never arrives, the generator **ends the stream cleanly** (logs a
    warning, returns) instead of raising mid-response — a raise after the 206 surfaces as an ugly
    ASGI `ExceptionGroup` and an alarming log; a clean end just lets the player retry (which
    succeeds once more of the file is cached). Playback therefore works even without port-forwarding,
    just slower on first play.

    Maintains a sliding window of boosted+deadlined pieces ahead of the read position. The window
    is a fixed *byte budget* (not a piece count) so on big torrents with large pieces it stays a
    tight ~50 MiB region — a seek rushes the first piece at the target instead of spreading
    bandwidth over ~1 GB."""
    try:
        plen = handle.piece_length()
        base = handle.file_offset(idx)
        path = file_disk_path(save_path, handle, idx)
        total = handle.num_pieces()
        window = max(4, min(total, window_bytes // plen))  # pieces, derived from the byte budget
        pos = start
        deadlined_to = (base + start) // plen - 1  # last piece we've already boosted
        yielded = False  # the first piece (cold start) gets the longer first_timeout budget
        while pos <= end:
            gp = (base + pos) // plen  # global piece index for the current byte position
            # Slide the boost window forward so upcoming pieces are rushed in order.
            far = min(gp + window, total - 1)
            while deadlined_to < far:
                deadlined_to += 1
                handle.boost_piece(deadlined_to, max(0, deadlined_to - gp) * step_ms)
            budget = timeout if yielded else first_timeout
            deadline = time.time() + budget
            wait_start = time.time()
            had_to_wait = not handle.have_piece(gp)  # piece not ready = playback waits for data
            while not handle.have_piece(gp) and time.time() < deadline:
                time.sleep(0.2)
            if not handle.have_piece(gp):
                # Give up gracefully: end the stream (no raise) so the player retries instead of
                # seeing an ASGI error. Common on peer-starved boxes — surfaced via /netcheck.
                metrics.record_timeout()
                logger.warning("piece %d not available within %.0fs (peer-starved?); ending stream", gp, budget)
                return
            if had_to_wait:
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
        # by the evictor mid-stream (invalid-handle), or a transient disk I/O error — would otherwise
        # surface as an ugly ASGI ExceptionGroup + nginx "upstream prematurely closed connection" and
        # alarm the user. End the stream cleanly instead; the player simply re-requests the range and
        # succeeds once the data is present. (Client disconnects raise GeneratorExit, not Exception,
        # so they pass through and close the generator normally.)
        metrics.record_timeout()
        logger.warning("stream ended early (%s: %s); player will retry", type(e).__name__, e)
        return
