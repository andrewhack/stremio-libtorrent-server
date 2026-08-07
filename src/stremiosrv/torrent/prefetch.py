"""Next-episode prefetch — pure helpers (no libtorrent, no engine state).

While the last part of an episode plays AND that episode is fully on disk, the server pulls the
head (plus the trailing index) of the next video file in the SAME torrent, so pressing Next starts
instantly instead of buffering. Scope is one torrent with many files: a next episode living in a
separate torrent has an infohash the server has never been told (the protocol carries only
/{infoHash}/{idx} plus a Range header), so it is out of reach here by construction.
"""
from __future__ import annotations

import os
import re

from stremiosrv.stream.fileserver import VIDEO_TYPES
from stremiosrv.torrent.picker import pieces_for_range

VIDEO_EXTENSIONS = frozenset(VIDEO_TYPES)
# Absolute floor for "this is an episode, not a sample". Paired with a relative floor below.
MIN_VIDEO_BYTES = 16 * 1024 * 1024
# An MP4 with a trailing moov makes the player seek to the end the instant the file opens, so a
# head-only prefetch would leave that stall in place. One or two pieces buys it back.
TAIL_BYTES = 4 * 1024 * 1024

_DIGITS = re.compile(r"(\d+)")


def natural_key(path: str) -> list:
    """Sort key that compares digit runs numerically, so 'Ep 2' precedes 'Ep 10'.

    re.split with a capture group always alternates non-digit / digit, so element i has the same
    type in every key (even = str, odd = int) and comparisons never mix str with int.
    """
    return [int(p) if p.isdigit() else p.lower() for p in _DIGITS.split(path)]


def position_reached(read_pos: int, total: int, trigger_fraction: float) -> bool:
    """Has the read cursor passed the trigger point of the file being played?

    A misconfigured fraction disables the feature rather than enabling it: a prefetch that never
    fires is a non-event, one that fires at every position is a bandwidth bug.
    """
    if total <= 0 or not (0 < trigger_fraction <= 1):
        return False
    return read_pos >= total * trigger_fraction


def next_video_index(paths: list[str], sizes: list[int], current: int) -> int | None:
    """Index of the video file following `current` in natural order, or None.

    Files under a quarter of the current file's size are dropped, which removes samples and extras
    with no filename heuristic and self-scales from a 150 MB episode to a 4 GB one. Returns None
    when `current` is not itself an eligible video, when it is the last one, or when the torrent
    holds a single video — which is how movies opt out without the server knowing what a TV show is.
    """
    if not (0 <= current < len(paths)) or current >= len(sizes):
        return None
    floor = max(MIN_VIDEO_BYTES, sizes[current] // 4)
    eligible = [
        i for i in range(min(len(paths), len(sizes)))
        if os.path.splitext(paths[i])[1].lower() in VIDEO_EXTENSIONS and sizes[i] >= floor
    ]
    eligible.sort(key=lambda i: natural_key(paths[i]))
    if current not in eligible:
        return None
    at = eligible.index(current)
    return eligible[at + 1] if at + 1 < len(eligible) else None


def head_pieces(file_offset: int, file_size: int, piece_length: int,
                fraction: float, max_bytes: int) -> list[int]:
    """Global piece indices covering the first min(fraction*size, max_bytes) bytes of a file.

    A fraction above 1 is rejected the same way position_reached rejects one outside (0, 1]:
    misconfiguration (e.g. a "5" meant as "5 percent") disables the feature instead of being bounded
    only by max_bytes — and not bounded at all if max_bytes is raised too.
    """
    if piece_length <= 0 or file_size <= 0 or fraction > 1:
        return []
    n = min(int(file_size * fraction), max_bytes, file_size)
    if n <= 0:
        return []
    return pieces_for_range(file_offset, file_offset + n - 1, piece_length)


def tail_pieces(file_offset: int, file_size: int, piece_length: int) -> list[int]:
    """Global piece indices covering the last TAIL_BYTES of a file (trailing moov / cues)."""
    if piece_length <= 0 or file_size <= 0:
        return []
    n = min(TAIL_BYTES, file_size)
    end = file_offset + file_size - 1
    return pieces_for_range(end - n + 1, end, piece_length)
