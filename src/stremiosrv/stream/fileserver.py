"""Serve byte ranges of a torrent file, waiting for the covering pieces to download.

Disk-read strategy: libtorrent writes pieces into `save_path/<file_path>`; once a piece is
present (`have_piece`) we read that region straight off disk. Pieces over the requested range
are raised to top priority by the caller (sequential "head & holes").
"""
from __future__ import annotations

import mimetypes
import os
import time
from collections.abc import Iterator

from stremiosrv import metrics

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
    timeout: float = 30.0, chunk: int = 262144, window_bytes: int = 50_331_648, step_ms: int = 50,
) -> Iterator[bytes]:
    """Yield bytes [start, end] (inclusive, file-relative) of file `idx`, blocking per chunk
    until the covering piece is available. Raises TimeoutError if a piece never arrives.

    Maintains a sliding window of boosted+deadlined pieces ahead of the read position. The window
    is a fixed *byte budget* (not a piece count) so on big torrents with large pieces it stays a
    tight ~50 MiB region — a seek rushes the first piece at the target instead of spreading
    bandwidth over ~1 GB."""
    plen = handle.piece_length()
    base = handle.file_offset(idx)
    path = file_disk_path(save_path, handle, idx)
    total = handle.num_pieces()
    window = max(4, min(total, window_bytes // plen))  # pieces, derived from the byte budget
    pos = start
    deadlined_to = (base + start) // plen - 1  # last piece we've already boosted
    while pos <= end:
        gp = (base + pos) // plen  # global piece index for the current byte position
        # Slide the boost window forward so upcoming pieces are rushed in order.
        far = min(gp + window, total - 1)
        while deadlined_to < far:
            deadlined_to += 1
            handle.boost_piece(deadlined_to, max(0, deadlined_to - gp) * step_ms)
        deadline = time.time() + timeout
        wait_start = time.time()
        had_to_wait = not handle.have_piece(gp)  # piece not ready = playback waits for data
        while not handle.have_piece(gp) and time.time() < deadline:
            time.sleep(0.2)
        if not handle.have_piece(gp):
            metrics.record_timeout()
            raise TimeoutError(f"piece {gp} not available within {timeout}s")
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
        pos += len(data)
