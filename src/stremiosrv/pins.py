"""Pinned-torrent registry + disk guard (pure helpers; no libtorrent here).

A pin keeps a torrent fully downloaded, never evicted, and seeding. Pins are recorded in
<cache_root>/pins.json so they survive restarts. Content-neutral: infohashes + names only.
"""
from __future__ import annotations

import json
import math
import os

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
