"""Pinned-torrent registry + disk guard (pure helpers; no libtorrent here).

A pin keeps a torrent fully downloaded, never evicted, and seeding. Pins are recorded in
<cache_root>/pins.json so they survive restarts. Content-neutral: infohashes + names only.
"""
from __future__ import annotations

import json
import math
import os
import re

PINS_FILE = "pins.json"


def _path(cache_root: str) -> str:
    return os.path.join(cache_root, PINS_FILE)


def load_pins(cache_root: str) -> list[dict]:
    """Pinned entries [{infoHash, name, trackers, addedAt}], or [] if absent/unreadable."""
    try:
        with open(_path(cache_root), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_pins(cache_root: str, entries: list[dict]) -> None:
    """Atomically write the pins registry."""
    tmp = _path(cache_root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    os.replace(tmp, _path(cache_root))


def pinned_hashes(cache_root: str) -> set[str]:
    return {e["infoHash"].lower() for e in load_pins(cache_root) if e.get("infoHash")}


def headroom(cache_size: int) -> int:
    """Bytes to keep free for normal streaming: cache budget + 10%."""
    return math.ceil(cache_size * 1.10)


def pin_fits(disk_free: int, pinned_remaining: int, candidate_remaining: int,
             cache_size: int) -> bool:
    """True if completing all pins (existing incomplete + candidate) still leaves >= headroom free."""
    return disk_free - (pinned_remaining + candidate_remaining) >= headroom(cache_size)


# --- which file a pin wants -------------------------------------------------------------------
# A pin used to mean "every file", so choosing one episode fetched the whole season pack -- tens of
# gigabytes for one of them, admitted past a disk guard that cannot see a magnet's size yet. The
# choice is already known at request time (the label carries season and episode, and an addon
# stream may carry an explicit fileIdx); it just never reached the engine.

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm", ".m2ts")


def select_wanted_file(paths: list[str], want: dict | None) -> int | None:
    """Index of the single file this pin wants, or None meaning "all of them".

    None is the safe answer, not a failure: a film in a folder, or a pack that numbers its
    episodes some way we do not recognise, must still land on disk in full rather than leave the
    owner with an empty directory.
    """
    if not want:
        return None
    idx = want.get("fileIdx")
    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(paths):
        return idx
    season, episode = want.get("season"), want.get("episode")
    if season is None or episode is None:
        return None
    try:
        s, e = int(season), int(episode)
    except (TypeError, ValueError):
        return None
    # The trailing (?!\d) is the whole point: without it S01E1 also matches S01E10, and the wrong
    # episode downloads with nothing to show that it did.
    pats = [
        re.compile(rf"s0*{s}[\s._-]*e0*{e}(?!\d)", re.IGNORECASE),
        re.compile(rf"(?<!\d)0*{s}\s*x\s*0*{e}(?!\d)", re.IGNORECASE),
    ]
    hits = [i for i, path in enumerate(paths)
            if any(p.search(os.path.basename(path)) for p in pats)]
    if not hits:
        return None
    videos = [i for i in hits if paths[i].lower().endswith(VIDEO_EXT)]
    return (videos or hits)[0]
