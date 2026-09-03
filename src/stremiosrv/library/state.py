"""One payload describing everything the box is holding, for the page to render.

Three sources are merged: what is on disk (cache.scan_cache), what is pinned and live in the engine
(engine.pinned_status), and what we know the titles of (labels.json).

**Everything on disk is returned, labelled or not.** The page renders unlabelled entries in their
own section. A view that showed only recognised titles would let the disk fill invisibly, which is
exactly the failure the fail-loud rule exists to prevent.
"""
from __future__ import annotations

import logging
import re

from stremiosrv import cache as cachemod
from stremiosrv.library import labels as labelsmod

log = logging.getLogger(__name__)

# libtorrent writes `.<infohash>.parts` beside the data of a partially-downloaded torrent. It is
# engine bookkeeping, not something the owner downloaded: shown as its own card it invites a delete
# that corrupts the torrent it belongs to. Its bytes are real though, so they are attributed to the
# entry they belong to rather than dropped -- silently under-reporting disk use is the failure this
# whole view exists to prevent.
_PARTFILE_RE = re.compile(r"^\.([0-9a-fA-F]{40})\.parts$")

# Below both of these, what is on disk for a file is boundary spill from its neighbours rather
# than something worth naming. A file the owner actually asked for is always listed, however
# little of it has arrived -- a download that has just started is not a scrap.
FRAGMENT_BYTES = 64 * 1024 * 1024
FRAGMENT_FRACTION = 0.02


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
        # tracked, not pinned: a download in flight is not pinned any more, and reading only the
        # pins would leave it invisible until its bytes reached the disk.
        pins = {p["infoHash"].lower(): p for p in (engine.tracked_status() or [])
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

    items = cachemod.scan_cache(cache_root)
    part_bytes: dict[str, int] = {}
    for item in items:
        m = _PARTFILE_RE.match(item["name"])
        if m:
            part_bytes[m.group(1).lower()] = part_bytes.get(m.group(1).lower(), 0) + item["size"]

    for item in items:
        name = item["name"]
        if _PARTFILE_RE.match(name):
            continue  # folded into its torrent below, or surfaced as an orphan at the end
        ih = (names.get(name) or idle.get(name) or "").lower()
        pin = pins.get(ih, {})
        if ih:
            seen.add(ih)
        entries.append({
            "name": name,
            "infoHash": ih or None,
            "size": item["size"] + part_bytes.pop(ih, 0) if ih else item["size"],
            "mtime": item["mtime"],
            # Kept, not merely present: the record says so now, because being tracked no longer
            # implies being pinned.
            "pinned": bool(pin.get("pinned")),
            # /library/api/remove is keyed by infohash; without one the button would do nothing.
            "removable": bool(ih),
            # Without a pin record there is no progress figure to report. The files are on disk and
            # nothing is downloading them, so treat them as complete rather than as 0% — a finished
            # entry showing "0%" reads as a stalled download.
            "progress": pin.get("progress", 1.0),
            # The one file a narrowed pin fetches, when it is narrower than the torrent.
            "wantedFile": pin.get("wantedFile"),
            "state": pin.get("state", "idle"),
            "peers": pin.get("peers", 0),
            "seeds": pin.get("seeds", 0),
            "downloadSpeed": pin.get("downloadSpeed", 0),
            "uploaded": pin.get("uploaded", 0),
            "ratio": pin.get("ratio", 0.0),
            "uploadSpeed": pin.get("uploadSpeed", 0),
            "label": all_labels.get(ih) if ih else None,
        })
        # A pack holds several episodes, arriving from different places -- one downloaded here,
        # another streamed by the player, possibly by different people. One entry per torrent
        # could not say that, so the disk grew with nothing on the page accounting for it. Split
        # the entry into the files it actually holds, keeping the torrent entry as their parent.
        # A piece straddles the boundary between two files, so fetching one leaves kilobytes --
        # occasionally a few megabytes -- of its neighbours behind. Those are not episodes anyone
        # has, and listing them as though they were is the same lie as hiding the real ones, told
        # the other way round. Show what someone could actually watch; account for the rest in one
        # line rather than dropping it, because unattributed disk is what this view exists to stop.
        held = [f for f in (pin.get("files") or []) if f.get("downloaded")]
        files = [f for f in held
                 if f.get("wanted") or f["downloaded"] >= FRAGMENT_BYTES
                 or f.get("progress", 0) >= FRAGMENT_FRACTION]
        scraps = [f for f in held if f not in files]
        if len(files) > 1 or (files and scraps):
            parent = entries[-1]
            if scraps:
                parent["scraps"] = {"count": len(scraps),
                                    "size": sum(f["downloaded"] for f in scraps)}
            parent["children"] = [{
                "name": f["name"],
                "infoHash": ih,
                "fileIdx": f["index"],
                "size": f["downloaded"],
                "progress": f["progress"],
                "wanted": f.get("wanted", False),
                "pinned": parent["pinned"],
                # Removal is still per-torrent: these share one handle and one directory, so
                # deleting one would have to stop the torrent the others are being read from.
                "removable": False,
            } for f in files]

    # A pin whose files have not landed yet has no cache directory. Without this the UI shows
    # nothing after a download is started and the click looks like it did nothing.
    for ih, pin in pins.items():
        if ih in seen:
            continue
        entries.append({
            "name": pin.get("name", ""), "infoHash": ih, "size": 0, "mtime": 0,
            "pinned": bool(pin.get("pinned")), "progress": pin.get("progress", 0.0),
            "wantedFile": pin.get("wantedFile"),
            "state": pin.get("state", "downloading"), "peers": pin.get("peers", 0),
            "seeds": pin.get("seeds", 0), "downloadSpeed": pin.get("downloadSpeed", 0),
            "uploaded": pin.get("uploaded", 0), "ratio": pin.get("ratio", 0.0),
            "uploadSpeed": pin.get("uploadSpeed", 0), "label": all_labels.get(ih),
            "removable": True,
        })

    # Partfiles whose torrent is no longer on disk. They still occupy space -- one on a real box
    # held 30 GB -- so hiding them would recreate the invisible-disk problem. They ARE removable:
    # /library/api/remove stops the torrent before deleting anything, and an orphan has no torrent
    # in the session at all, so there is nothing to corrupt.
    for ih, size in part_bytes.items():
        entries.append({
            "name": f"incomplete download data ({ih[:8]})", "infoHash": ih, "size": size,
            "mtime": 0, "pinned": False, "progress": 0.0, "state": "idle", "peers": 0,
            "seeds": 0, "downloadSpeed": 0,
            "uploaded": 0, "ratio": 0.0, "uploadSpeed": 0, "label": None, "removable": True,
        })

    # cache.usage already reports the budget as `cacheSize`; adding a second key for it would give
    # one number two names, which is how the two spellings drift apart.
    return {"entries": entries, "budget": cachemod.usage(cache_root, budget)}
