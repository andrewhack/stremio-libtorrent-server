"""OpenSubtitles file hash — the de-facto matching key used by subtitle addons.

hash = (filesize + Σ int64le(first 64 KiB) + Σ int64le(last 64 KiB)) mod 2^64, as 16-char hex.
Pure (operates on a file path) and unit-testable with synthetic files.
"""
from __future__ import annotations

import os
import struct

_CHUNK = 65536
_MASK = 0xFFFFFFFFFFFFFFFF


def _sum_int64le(fh, count: int) -> int:
    total = 0
    for _ in range(count):
        buf = fh.read(8)
        if len(buf) < 8:
            break
        total += struct.unpack("<Q", buf)[0]
    return total


def opensubtitles_hash_and_size(path: str) -> tuple[str, int]:
    """Return (16-hex hash, file size in bytes) from a SINGLE stat of the file.

    OpenSubtitles matches on BOTH the moviehash and the moviebytesize, and the byte size is itself a
    term of the hash — so the size we report to the client MUST be the exact size the hash was
    computed against. Returning them together guarantees that; a size that disagrees with the hashed
    size never matches."""
    fsize = os.path.getsize(path)
    h = fsize
    with open(path, "rb") as fh:
        h += _sum_int64le(fh, _CHUNK // 8)
        if fsize > _CHUNK:
            fh.seek(max(0, fsize - _CHUNK), os.SEEK_SET)
            h += _sum_int64le(fh, _CHUNK // 8)
    return f"{h & _MASK:016x}", fsize


def opensubtitles_hash(path: str) -> str:
    """The 16-hex OpenSubtitles hash alone (see opensubtitles_hash_and_size)."""
    return opensubtitles_hash_and_size(path)[0]
