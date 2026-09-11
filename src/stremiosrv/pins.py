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
    is_episode = _episode_matcher(want)
    if is_episode is None:
        return None
    hits = [i for i, path in enumerate(paths) if is_episode(path)]
    if not hits:
        return None
    videos = [i for i in hits if paths[i].lower().endswith(VIDEO_EXT)]
    return (videos or hits)[0]


def _episode_matcher(want: dict | None):
    """A test for "this path is the wanted episode", or None when `want` names no episode."""
    if not want:
        return None
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
    return lambda path: any(p.search(os.path.basename(path)) for p in pats)


# --- which file a stream wants when the addon does not say ------------------------------------
# A stream with no fileIdx leaves the choice to the server: stremio-video asks for a guess in the
# body of POST /<ih>/create (`guessFileIdx`, with season and episode when it has them), and
# stremio-core writes -1 into the URLs it builds. Both get the stock server's answer (GuessFileIdx
# in server.reference.js): among the media files, the wanted episode, else the largest.

# The stock server's media list plus the video types this module already knew. Audio counts: a
# torrent of music is still something to play.
MEDIA_EXT = (*VIDEO_EXT, ".wmv", ".vp8", ".mpg", ".m3u8", ".flac", ".mp3", ".wav", ".wma",
             ".aac", ".ogg")


def guess_file_idx(files: list[tuple[str, int]], want: dict | None = None) -> int:
    """Index of the file to play among `(path, size)` pairs, or -1 when none of them is media.

    The largest media file wins, the first on a tie; when `want` names a season and episode, the
    largest file of that episode wins instead, if there is one. Unlike the stock server this
    honours season 0, the catalog's specials, which it discards as falsy.
    """
    media = [i for i, (path, _size) in enumerate(files) if path.lower().endswith(MEDIA_EXT)]
    is_episode = _episode_matcher(want)
    if is_episode is not None:
        media = [i for i in media if is_episode(files[i][0])] or media
    best = -1
    for i in media:
        if best < 0 or files[i][1] > files[best][1]:
            best = i
    return best
