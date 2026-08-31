"""One payload describing everything the box is holding, for the page to render.

Three sources are merged: what is on disk (cache.scan_cache), what is pinned and live in the engine
(engine.pinned_status), and what we know the titles of (labels.json).

**Everything on disk is returned, labelled or not.** The page renders unlabelled entries in their
own section. A view that showed only recognised titles would let the disk fill invisibly, which is
exactly the failure the fail-loud rule exists to prevent.
"""
from __future__ import annotations

import logging

from stremiosrv import cache as cachemod
from stremiosrv.library import labels as labelsmod

log = logging.getLogger(__name__)


def _engine_view(engine) -> tuple[dict, dict]:
    """(name -> infohash, infohash -> pin status). Never raises: the engine is allowed to be absent
    or briefly broken, and a listing of the disk is still worth serving when it is."""
    if engine is None:
        return {}, {}
    try:
        names = {n: h.lower() for n, h in (engine.name_to_hash() or {}).items()}
    except Exception as e:  # noqa: BLE001 — degrade to a disk-only listing
        log.warning("library: name_to_hash failed: %s: %s", type(e).__name__, e)
        names = {}
    try:
        pins = {p["infoHash"].lower(): p for p in (engine.pinned_status() or [])
                if p.get("infoHash")}
    except Exception as e:  # noqa: BLE001 — degrade to a disk-only listing
        log.warning("library: pinned_status failed: %s: %s", type(e).__name__, e)
        pins = {}
    return names, pins


def build(cache_root: str, engine, budget: int = 0) -> dict:
    names, pins = _engine_view(engine)
    idle = cachemod.load_name_index(cache_root)
    all_labels = labelsmod.load(cache_root)
    entries: list[dict] = []
    seen: set[str] = set()

    for item in cachemod.scan_cache(cache_root):
        name = item["name"]
        ih = (names.get(name) or idle.get(name) or "").lower()
        pin = pins.get(ih, {})
        if ih:
            seen.add(ih)
        entries.append({
            "name": name,
            "infoHash": ih or None,
            "size": item["size"],
            "mtime": item["mtime"],
            "pinned": bool(pin),
            # Without a pin record there is no progress figure to report. The files are on disk and
            # nothing is downloading them, so treat them as complete rather than as 0% — a finished
            # entry showing "0%" reads as a stalled download.
            "progress": pin.get("progress", 1.0),
            "state": pin.get("state", "idle"),
            "peers": pin.get("peers", 0),
            "uploaded": pin.get("uploaded", 0),
            "ratio": pin.get("ratio", 0.0),
            "uploadSpeed": pin.get("uploadSpeed", 0),
            "label": all_labels.get(ih) if ih else None,
        })

    # A pin whose files have not landed yet has no cache directory. Without this the UI shows
    # nothing after a download is started and the click looks like it did nothing.
    for ih, pin in pins.items():
        if ih in seen:
            continue
        entries.append({
            "name": pin.get("name", ""), "infoHash": ih, "size": 0, "mtime": 0,
            "pinned": True, "progress": pin.get("progress", 0.0),
            "state": pin.get("state", "downloading"), "peers": pin.get("peers", 0),
            "uploaded": pin.get("uploaded", 0), "ratio": pin.get("ratio", 0.0),
            "uploadSpeed": pin.get("uploadSpeed", 0), "label": all_labels.get(ih),
        })

    # cache.usage already reports the budget as `cacheSize`; adding a second key for it would give
    # one number two names, which is how the two spellings drift apart.
    return {"entries": entries, "budget": cachemod.usage(cache_root, budget)}
