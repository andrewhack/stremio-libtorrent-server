"""infohash -> title record, so the cache can be shown as titles rather than folder names.

Written when a download is started from the page, which is the one moment the title is known.
Entries the server has no label for are labelled client-side from the web player's own `streams` and
`library` buckets, so this file is a convenience for cross-device display, not the source of truth.

**This is the owner's library, on the owner's own box.** It is never logged, never included in
release notes or issue reports, and never served on an unauthenticated route. Same discipline as
pins.py, which already stores torrent names.
"""
from __future__ import annotations

import json
import os
import threading
import time

LABELS_FILE = "labels.json"

# `put` is read-modify-write and the download endpoint runs in uvicorn's threadpool, so two
# downloads started together really do collide. Unsynchronised, 24 concurrent puts left **one**
# label — and sometimes zero, because every thread wrote the same `.tmp` path and renamed it under
# the others, producing invalid JSON that `load` then discarded wholesale. A lost label would be
# cosmetic; losing the whole file is not.
_lock = threading.Lock()

# Whitelist, not a blocklist: the payload comes from the browser, and storing whatever it sends
# would let a compromised page park arbitrary data — an authKey, say — in a file on the box.
FIELDS = ("metaId", "videoId", "type", "name", "season", "episode", "poster")


def _path(cache_root: str) -> str:
    return os.path.join(cache_root, LABELS_FILE)


def load(cache_root: str) -> dict:
    """All labels keyed by lowercase infohash, or {} if absent/unreadable."""
    try:
        with open(_path(cache_root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(cache_root: str, data: dict) -> None:
    """Write via a temp name unique to this thread, then rename.

    The uniqueness is not belt-and-braces on top of the lock, it is the other half of the fix: a
    shared `<file>.tmp` lets one writer's rename fire while another is still filling the same path,
    which on Windows raises PermissionError outright and elsewhere silently publishes a half-written
    file. The lock orders writers inside this process; the unique name keeps any writer that is not
    holding it — another process, a future caller — from corrupting the target.
    """
    tmp = f"{_path(cache_root)}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _path(cache_root))


def put(cache_root: str, info_hash: str, label: dict) -> None:
    entry = {k: label[k] for k in FIELDS if k in label}
    entry["addedAt"] = int(time.time())
    with _lock:
        data = load(cache_root)
        data[info_hash.lower()] = entry
        _save(cache_root, data)


def drop(cache_root: str, info_hash: str) -> None:
    with _lock:
        data = load(cache_root)
        if data.pop(info_hash.lower(), None) is not None:
            _save(cache_root, data)
