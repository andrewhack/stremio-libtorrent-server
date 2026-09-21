"""infohash -> title record, so the cache can be shown as titles rather than folder names.

Written when a download is started from the page, which is the one moment the page knows the
title, and learned at playback by the library's addon (see `learn` and `add_file`). For the page's
display it is a convenience: entries without a label are labelled client-side from the web player's
own `streams` and `library` buckets. For the addon it is the record: a learned label's `file` is the
one file a title's page offers.

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


# What a label learned at playback may carry. Built by the server from the player's report, never
# sent by the browser: `file` in particular is not in FIELDS, so the page cannot set it.
LEARNED_FIELDS = ("metaId", "videoId", "type", "season", "episode", "file")


def file_record(value: object) -> dict | None:
    """A label's `file` -- {"name": <basename>, "size": <bytes>} -- or None when it is not one.

    labels.json is a plain file on the owner's box and can be edited by hand. Anything but this
    exact shape reads as no file, and that label answers as one learned before files were recorded.
    """
    if not isinstance(value, dict):
        return None
    name, size = value.get("name"), value.get("size")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return None
    return {"name": name, "size": size}


def learn(cache_root: str, info_hash: str, label: dict) -> bool:
    """Write a label learned at playback, unless the torrent has one by now. True if written.

    The torrent was unlabelled when the state was read, but a download started from the page can
    label it before this runs -- and a label the page wrote is the owner's choice. `put` would
    overwrite it, so this checks again under the lock.
    """
    entry = {k: label[k] for k in LEARNED_FIELDS if k in label}
    found = file_record(entry.pop("file", None))
    if found is not None:
        entry["file"] = found
    entry["addedAt"] = int(time.time())
    with _lock:
        data = load(cache_root)
        if data.get(info_hash.lower()):
            return False
        data[info_hash.lower()] = entry
        _save(cache_root, data)
    return True


def add_file(cache_root: str, info_hash: str, label: dict) -> bool:
    """Record `label`'s file on the stored label of the same video, if that one has none yet. True
    if written.

    The same video is the same metaId, season and episode: a label records the file it was learned
    from, not every file played from its torrent. Nothing else in the stored label changes -- the
    page may have written it, with a name and a poster the owner chose. Checked again under the
    lock, because the page can replace a label between the state read and this write.
    """
    found = file_record(label.get("file"))
    if found is None:
        return False
    with _lock:
        data = load(cache_root)
        stored = data.get(info_hash.lower())
        if (not isinstance(stored, dict) or file_record(stored.get("file")) is not None
                or any(stored.get(k) != label.get(k) for k in ("metaId", "season", "episode"))):
            return False
        stored["file"] = found
        _save(cache_root, data)
    return True


def drop(cache_root: str, info_hash: str) -> None:
    with _lock:
        data = load(cache_root)
        if data.pop(info_hash.lower(), None) is not None:
            _save(cache_root, data)
