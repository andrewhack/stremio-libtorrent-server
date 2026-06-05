"""Serve byte ranges of a torrent file, waiting for the covering pieces to download.

Disk-read strategy: libtorrent writes pieces into `save_path/<file_path>`; once a piece is
present (`have_piece`) we read that region straight off disk. Pieces over the requested range
are raised to top priority by the caller (sequential "head & holes").
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator


def file_disk_path(save_path: str, handle, idx: int) -> str:
    return os.path.join(save_path, handle.file_path(idx))


def wait_and_read(
    save_path: str, handle, idx: int, start: int, end: int,
    timeout: float = 30.0, chunk: int = 262144, window: int = 64, step_ms: int = 50,
) -> Iterator[bytes]:
    """Yield bytes [start, end] (inclusive, file-relative) of file `idx`, blocking per chunk
    until the covering piece is available. Raises TimeoutError if a piece never arrives.

    Maintains a sliding window of piece *deadlines* ahead of the read position so libtorrent
    fetches the playhead region urgently and in order, regardless of where in the file we are."""
    plen = handle.piece_length()
    base = handle.file_offset(idx)
    path = file_disk_path(save_path, handle, idx)
    total = handle.num_pieces()
    pos = start
    deadlined_to = (base + start) // plen - 1  # last piece we've already set a deadline on
    while pos <= end:
        gp = (base + pos) // plen  # global piece index for the current byte position
        # Slide the deadline window forward so upcoming pieces are rushed in order.
        far = min(gp + window, total - 1)
        while deadlined_to < far:
            deadlined_to += 1
            handle.set_piece_deadline(deadlined_to, max(0, deadlined_to - gp) * step_ms)
        deadline = time.time() + timeout
        while not handle.have_piece(gp) and time.time() < deadline:
            time.sleep(0.2)
        if not handle.have_piece(gp):
            raise TimeoutError(f"piece {gp} not available within {timeout}s")
        n = min(chunk, end - pos + 1)
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read(n)
        if not data:
            break
        yield data
        pos += len(data)
