"""What a torrent is being fetched FOR — the files someone asked for, independent of pinning.

A download and a stream are the same operation: "this file of this torrent is wanted". The only
difference is urgency, which is already the active-vs-idle file priority. Pinning is a separate,
manual decision that means "do not evict"; inheriting it from a download is what made a library
download behave unlike ordinary playback, and it dragged a disk guard and an eviction exemption
along with it for no reason the owner ever asked for.

Two files of ONE torrent can be wanted at the same time -- one person downloading an episode while
another streams a different one -- so this is a set per torrent, never a single index.

Only explicit downloads are persisted. A file wanted because someone is streaming it stays wanted
for the life of the process (so simultaneous viewers do not fight, and switching episodes does not
throw away what was already fetched), but it is not resurrected on restart: incidental playback
should not commit the box to finishing every file it ever touched.

Content-neutral: infohashes and file selectors only.
"""
from __future__ import annotations

import json
import os

WANTED_FILE = "wanted.json"


def _path(cache_root: str) -> str:
    return os.path.join(cache_root, WANTED_FILE)


def load(cache_root: str) -> dict[str, list[dict]]:
    """{infohash: [selector, ...]}, or {} if absent/unreadable."""
    try:
        with open(_path(cache_root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for ih, specs in data.items():
        if isinstance(specs, list):
            out[str(ih).lower()] = [s for s in specs if isinstance(s, dict)]
    return out


def save(cache_root: str, data: dict[str, list[dict]]) -> None:
    """Atomically write the registry. Best-effort: a read-only cache root must not stop a
    download, it only means the selection will not survive a restart."""
    tmp = _path(cache_root) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _path(cache_root))
    except OSError:
        pass


def add(cache_root: str, info_hash: str, spec: dict | None) -> None:
    """Record that `spec` of this torrent is wanted. A None spec means the whole torrent.

    Duplicates are dropped rather than accumulated: clicking Download twice on the same release
    must not grow the file without bound.
    """
    ih = info_hash.lower()
    data = load(cache_root)
    specs = data.get(ih, [])
    entry = spec or {}
    if entry not in specs:
        specs.append(entry)
    data[ih] = specs
    save(cache_root, data)


def drop(cache_root: str, info_hash: str) -> None:
    """Forget every selection for this torrent -- it is being removed."""
    ih = info_hash.lower()
    data = load(cache_root)
    if data.pop(ih, None) is not None:
        save(cache_root, data)
