"""Cache eviction: keep the download cache under a size budget by deleting least-recently-used
media, like the stock server's cacheSize behaviour. Protects the TLS cert, settings, the transcode
working dir, and anything actively streaming.

`select_evictions` and `scan_cache` are pure-ish (filesystem only) and unit-testable; `run_evictor`
is the background loop wired in by the server entrypoint.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import time
import uuid

from stremiosrv import pins as pinsmod

logger = logging.getLogger("stremiosrv.cache")

# libtorrent's holding file for a torrent's partial pieces, named from the infohash. It is a
# top-level cache entry in its own right, and its name can never equal a torrent's name -- so
# every protection built from torrent names missed it completely.
PARTFILE_RE = re.compile(r"^\.([0-9a-f]{40})\.parts$", re.IGNORECASE)


def partfile_hash(name: str) -> str | None:
    m = PARTFILE_RE.match(name)
    return m.group(1).lower() if m else None

# Never evict these top-level entries.
PROTECTED = frozenset({
    "certificates.pem",
    "httpsCert.json",
    "server-settings.json",
    ".server-settings.json.swp",
    "stremio-cache",
    "transcode",
    ".resume",
    "pins.json",
    # Library-UI state, for the same reason pins.json is here. Both live in cache_root beside the
    # torrent data, and both are small and rarely rewritten — so select_evictions, which sorts by
    # mtime, would pick them FIRST once the cache went over budget. Losing library-ui.json is not a
    # cosmetic loss: load_state falls back to a blank owner_id, so the owner pin disappears and the
    # next Stremio account to sign in claims the server.
    "labels.json",
    # Which files each torrent is being fetched for. Losing it abandons every download in
    # flight at the next restart, silently and mid-file.
    "wanted.json",
    "library-ui.json",
    # Which server is allowed to evict from this root — see evictor_may_run.
    ".evictor-owner",
})

# --- cache-root ownership ---------------------------------------------------------------------
# One cache root, one evictor. Two servers pointed at the same directory do not share a view of
# it: each protects only what ITS libtorrent session holds a handle for, and each enforces ITS
# own budget. The smaller budget therefore deletes the larger server's cache wholesale, and the
# entries all read [no-handle] on the way out because they were never this process's to begin
# with. That is not a hypothetical -- it took a live cache to almost nothing in one pass.
#
# The claim is advisory and self-expiring: a heartbeat file, refreshed each pass, that goes stale
# when its holder stops. Losing eviction is a recoverable, loud failure (the over-budget warning
# below fires every pass); losing the cache is not.
OWNER_FILE = ".evictor-owner"
_TOKEN = uuid.uuid4().hex  # this process's identity, minted once per interpreter
# A restart mints a new token but keeps the hostname, and a container's own dead process is not a
# rival -- without this the survivor of a restart locks itself out of its own cache root.
_HOST = socket.gethostname()


def read_owner(root: str) -> dict | None:
    """The current claim on `root`, or None if unclaimed/unreadable."""
    try:
        with open(os.path.join(root, OWNER_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_owner(root: str, token: str | None = None, now: float | None = None) -> None:
    """Claim `root`, or refresh an existing claim. Best-effort: a read-only root must not stop
    the server, it only means the guard cannot help there."""
    rec = {"token": token or _TOKEN, "host": _HOST, "pid": os.getpid(),
           "heartbeat": now or time.time()}
    path = os.path.join(root, OWNER_FILE)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("could not claim cache root %s: %s", root, e)


def evictor_may_run(root: str, stale_after: float) -> tuple[bool, dict | None]:
    """(may this process evict from `root`, the rival claim that says otherwise).

    Ours or absent or stale -> yes, and the claim is taken. A live claim by another token -> no.
    """
    rec = read_owner(root)
    if rec and rec.get("token") != _TOKEN and rec.get("host") != _HOST:
        age = time.time() - float(rec.get("heartbeat") or 0)
        if age <= stale_after:
            return False, rec
    write_owner(root)
    return True, None



def _real_size(st) -> int:
    """Bytes a file actually occupies on disk, not what it will eventually be.

    libtorrent pre-allocates the WHOLE torrent as a sparse file the moment it is added, so `st_size`
    reports the finished size from the start and counts data nobody has downloaded yet. A
    64%-complete 86.2 GiB torrent measured 92,598,768,617 by `st_size` and 56 GiB by `du` — the
    evictor was deciding on a figure 31 GiB larger than the disk agreed with, and the operator was
    reading an over-budget warning inflated by the same amount.

    `min` of apparent and allocated rather than `st_blocks` alone: allocation rounds up to the block
    size, so a 500-byte file would otherwise be billed 4 KiB, and a cache of many small files would
    drift the other way. Under-counting slightly is the safe direction for a budget whose whole job
    is predicting disk pressure. `st_blocks` is POSIX-only — absent on Windows, where the dev tests
    run and where nothing is sparse anyway, so apparent size is the correct fallback there.
    """
    blocks = getattr(st, "st_blocks", None)
    return st.st_size if blocks is None else min(st.st_size, blocks * 512)


def _stat_tree(path: str) -> tuple[int, float]:
    """(total size in bytes, newest mtime) for a file or directory tree."""
    if os.path.isfile(path):
        st = os.stat(path)
        return _real_size(st), st.st_mtime
    total = 0
    latest = 0.0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            total += _real_size(st)
            latest = max(latest, st.st_mtime)
    return total, latest


NAME_INDEX = ".resume/index.json"  # relative to cache_root: {torrent_name: infohash} for cached torrents


def load_name_index(root: str) -> dict:
    """Persisted name->infohash map (so idle/unloaded cache items can still be pinned). {} on error."""
    try:
        with open(os.path.join(root, NAME_INDEX), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_name_index(root: str, mapping: dict) -> None:
    """Atomically write the name->infohash index (under the eviction-protected .resume dir)."""
    path = os.path.join(root, NAME_INDEX)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mapping, f)
    os.replace(tmp, path)


def scan_cache(root: str, protected: frozenset[str] = PROTECTED) -> list[dict]:
    """List evictable cache entries: {name, path, size, mtime}. Skips protected names."""
    items: list[dict] = []
    try:
        names = os.listdir(root)
    except OSError:
        return items
    for name in names:
        if name in protected:
            continue
        path = os.path.join(root, name)
        size, mtime = _stat_tree(path)
        items.append({"name": name, "path": path, "size": size, "mtime": mtime})
    return items


def usage(root: str, budget: int) -> dict:
    """Cache footprint vs budget + free disk — for the appliance suggestion advisor.

    `cacheUsed` sums the same evictable entries the evictor manages (protected names excluded), so
    it stays comparable with `cacheSize`. `transcodeUsed` is reported *beside* it rather than folded
    into it, because the two answer different questions and merging them would make both useless:
    transcode output is not evictable (it is in PROTECTED), so adding it to `cacheUsed` would show
    the cache over budget while the evictor correctly refused to act.

    It is reported at all because leaving it out was actively misleading. HLS segments land under
    `<cache_root>/transcode`, which `scan_cache` skips — so a disk could fill with segment files
    while `cacheUsed` still read comfortably under budget and nothing in the response accounted for
    the difference. `diskFree`/`diskTotal` showed the symptom; this names the cause.
    """
    used = sum(i["size"] for i in scan_cache(root))
    transcode, _ = _stat_tree(os.path.join(root, "transcode"))
    try:
        du = shutil.disk_usage(root)
        free, total = du.free, du.total
    except OSError:
        free, total = 0, 0
    return {
        "cacheUsed": used, "cacheSize": budget, "transcodeUsed": transcode,
        "diskFree": free, "diskTotal": total,
    }


def select_evictions(
    items: list[dict], budget: int, in_use: frozenset[str] = frozenset(), target_ratio: float = 0.9,
) -> list[dict]:
    """Pick least-recently-modified items to delete so total falls to ~target_ratio*budget.
    Never selects in-use names. Pure: no side effects."""
    total = sum(i["size"] for i in items)
    if total <= budget:
        return []
    target = int(budget * target_ratio)
    candidates = sorted((i for i in items if i["name"] not in in_use), key=lambda i: i["mtime"])
    out: list[dict] = []
    freed = 0
    for it in candidates:
        if total - freed <= target:
            break
        out.append(it)
        freed += it["size"]
    return out


def _remove(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def evict_once(root: str, budget: int, engine=None, grace: int = 300) -> dict:
    """One eviction pass. Returns {before, after, deleted:[{name,size}]}."""
    items = scan_cache(root)
    total = sum(i["size"] for i in items)
    # Protect files modified within `grace` (actively downloading), even with no engine record yet.
    now = time.time()
    in_use = {i["name"] for i in items if now - i["mtime"] <= grace}
    name_hash: dict[str, str] = {}
    ages: dict[str, float] = {}
    if engine is not None:
        in_use |= set(engine.recent_names(grace))
        if hasattr(engine, "pinned_names"):
            in_use |= set(engine.pinned_names())
        name_hash = engine.name_to_hash()
        if hasattr(engine, "access_ages"):
            ages = engine.access_ages()
    # Pins are durable state; handles are not. `engine.pinned_names()` reports only torrents that
    # are loaded AND have metadata, so a pin stopped protecting anything the moment its torrent
    # was not in the session -- and a finished download, which nothing writes to any more, is the
    # oldest entry in the cache and therefore first in line. Read the registry instead.
    pin_entries = pinsmod.load_pins(root)
    keep_hashes = {(e.get("infoHash") or "").lower() for e in pin_entries if e.get("infoHash")}
    in_use |= {e["name"] for e in pin_entries if e.get("name")}
    # ...but `pin()` records the torrent name only when metadata had already arrived, and nothing
    # ever backfills it -- a magnet pinned the moment it is added carries "" for the life of the
    # pin, which is the normal case, not the corner one. The name index, written whenever resume
    # data is saved, knows the name; without this the durable protection above is inert for
    # exactly the pins that need it.
    by_hash = {str(h).lower(): n for n, h in load_name_index(root).items() if h}
    in_use |= {by_hash[ih] for ih in keep_hashes if ih in by_hash}
    # Give every partfile the protection its torrent has. Deleting one on its own throws away the
    # partial pieces of a torrent we just decided to keep, and it always looked evictable because
    # its name matches no torrent.
    keep_hashes |= {name_hash[n] for n in list(in_use) if n in name_hash}
    in_use |= {f".{ih}.parts" for ih in keep_hashes if ih}
    victims = select_evictions(items, budget, frozenset(in_use))
    # Over budget and nothing may be deleted. Previously this pass just did nothing and said
    # nothing, so the cache could sit above its budget indefinitely while the log looked idle —
    # and the bigger `grace` is, the likelier that becomes, because more items are protected.
    # It is not an error (protecting a stream is the correct call), but it must be visible: the
    # next thing that happens is the disk filling up.
    if total > budget and not victims:
        held = sum(i["size"] for i in items if i["name"] in in_use)
        logger.warning(
            "over budget by %.1f GiB and nothing is evictable: %d of %d items protected "
            "(%.1f GiB) by grace=%ss or pins — cache will stay over budget until one ages out",
            (total - budget) / 1073741824, len(in_use), len(items), held / 1073741824, grace,
        )
    deleted = []
    for v in victims:
        ih = name_hash.get(v["name"])
        if engine is not None and ih:
            engine.remove(ih)  # stop libtorrent before deleting its files
        _remove(v["path"])
        # ...and the holding file that belongs to it, or the next pass finds an orphan whose
        # torrent no longer exists and which nothing will ever claim.
        if ih:
            _remove(os.path.join(root, f".{ih}.parts"))
        deleted.append({"name": v["name"], "size": v["size"]})
        # Age is the diagnostic that was missing: "last served 41m ago" is a clean reclaim,
        # "last served 6m ago" means a viewer just lost their stream. `unserved` = never requested
        # from this process (a leftover on disk, or downloaded but never played).
        age = ages.get(v["name"])
        logger.info(
            "evicted %s [%s] (%.1f MiB, last served %s)",
            v["name"], ih or "no-handle", v["size"] / 1048576,
            f"{age / 60:.0f}m ago" if age is not None else "unserved",
        )
    return {"before": total, "after": total - sum(d["size"] for d in deleted), "deleted": deleted}


def run_evictor(root: str, budget: int, engine=None, interval: int = 60, grace: int = 300) -> None:
    """Background loop: evict over-budget cache every `interval` seconds. Runs forever."""
    if not logger.handlers:  # ensure visibility (uvicorn doesn't surface our INFO logs by default)
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [cache] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    # One evictor per cache root. A rival claim means another server is already managing this
    # directory with its own budget and its own idea of what is in use; running a second pass
    # here deletes that server's cache, not ours.
    stale_after = max(300.0, interval * 5)
    logger.info("cache evictor started: budget=%.1f GiB, interval=%ss", budget / 1073741824, interval)
    blocked = False
    while True:
        time.sleep(interval)  # sleep first: let active streams re-register after a restart
        # Re-checked every cycle rather than once at startup, and it doubles as the heartbeat on
        # our own claim. Giving up permanently turned an overlap of a few minutes into an evictor
        # that never ran again for the life of the process -- and a cache stuck over budget with
        # nothing in the log to say why.
        may, other = evictor_may_run(root, stale_after)
        if not may:
            if not blocked:
                logger.error(
                    "cache root %s is claimed by another server (%s, pid %s, last seen %.0fs "
                    "ago) — holding off. Two servers sharing one cache root delete each other's "
                    "data: give each container its own directory.",
                    # A claim written before this field existed has no host, and "on None" reads
                    # like a bug in the message rather than a fact about the claim.
                    root, other.get("host") or "host unknown", other.get("pid"),
                    time.time() - float(other.get("heartbeat") or 0),
                )
                blocked = True
            continue
        if blocked:
            logger.info("cache root %s is free again — resuming eviction", root)
            blocked = False
        try:
            res = evict_once(root, budget, engine, grace)
            if res["deleted"]:
                logger.info(
                    "evicted %d item(s), %.1f -> %.1f GiB",
                    len(res["deleted"]), res["before"] / 1073741824, res["after"] / 1073741824,
                )
        except Exception:
            logger.exception("eviction pass failed")
