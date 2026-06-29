"""Cache eviction: keep the download cache under a size budget by deleting least-recently-used
media, like the stock server's cacheSize behaviour. Protects the TLS cert, settings, the transcode
working dir, and anything actively streaming.

`select_evictions` and `scan_cache` are pure-ish (filesystem only) and unit-testable; `run_evictor`
is the background loop wired in by the server entrypoint.
"""
from __future__ import annotations

import logging
import os
import shutil
import time

logger = logging.getLogger("stremiosrv.cache")

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
})


def _stat_tree(path: str) -> tuple[int, float]:
    """(total size in bytes, newest mtime) for a file or directory tree."""
    if os.path.isfile(path):
        st = os.stat(path)
        return st.st_size, st.st_mtime
    total = 0
    latest = 0.0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            total += st.st_size
            latest = max(latest, st.st_mtime)
    return total, latest


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
    `cacheUsed` sums the same evictable entries the evictor manages (protected names excluded)."""
    used = sum(i["size"] for i in scan_cache(root))
    try:
        du = shutil.disk_usage(root)
        free, total = du.free, du.total
    except OSError:
        free, total = 0, 0
    return {"cacheUsed": used, "cacheSize": budget, "diskFree": free, "diskTotal": total}


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
    if engine is not None:
        in_use |= set(engine.recent_names(grace))
        name_hash = engine.name_to_hash()
    victims = select_evictions(items, budget, frozenset(in_use))
    deleted = []
    for v in victims:
        ih = name_hash.get(v["name"])
        if engine is not None and ih:
            engine.remove(ih)  # stop libtorrent before deleting its files
        _remove(v["path"])
        deleted.append({"name": v["name"], "size": v["size"]})
        logger.info("evicted %s (%.1f MiB)", v["name"], v["size"] / 1048576)
    return {"before": total, "after": total - sum(d["size"] for d in deleted), "deleted": deleted}


def run_evictor(root: str, budget: int, engine=None, interval: int = 60, grace: int = 300) -> None:
    """Background loop: evict over-budget cache every `interval` seconds. Runs forever."""
    if not logger.handlers:  # ensure visibility (uvicorn doesn't surface our INFO logs by default)
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [cache] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    logger.info("cache evictor started: budget=%.1f GiB, interval=%ss", budget / 1073741824, interval)
    while True:
        time.sleep(interval)  # sleep first: let active streams re-register after a restart
        try:
            res = evict_once(root, budget, engine, grace)
            if res["deleted"]:
                logger.info(
                    "evicted %d item(s), %.1f -> %.1f GiB",
                    len(res["deleted"]), res["before"] / 1073741824, res["after"] / 1073741824,
                )
        except Exception:  # noqa: BLE001 — never let the evictor thread die
            logger.exception("eviction pass failed")
        time.sleep(interval)
