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


def opensubtitles_hash(path: str) -> str:
    fsize = os.path.getsize(path)
    h = fsize
    with open(path, "rb") as fh:
        h += _sum_int64le(fh, _CHUNK // 8)
        if fsize > _CHUNK:
            fh.seek(max(0, fsize - _CHUNK), os.SEEK_SET)
            h += _sum_int64le(fh, _CHUNK // 8)
    return f"{h & _MASK:016x}"
