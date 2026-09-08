import os
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from stremiosrv import cache as cachemod

router = APIRouter()


class RemoveBody(BaseModel):
    name: str


_INFOHASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _name_to_hash(engine) -> dict:
    return engine.name_to_hash() if engine is not None else {}


@router.get("/cache.json")
def cache_list(request: Request) -> list[dict]:
    """On-disk cache entries (the LRU store) — distinct from /active.json (loaded torrents).
    Content-neutral: names only, the owner's own box."""
    s = request.app.state.settings
    live = _name_to_hash(request.app.state.engine)
    idle = cachemod.load_name_index(s.cache_root)
    out = []
    for item in cachemod.scan_cache(s.cache_root):
        ih = live.get(item["name"]) or idle.get(item["name"])
        out.append({
            "name": item["name"],
            "size": item["size"],
            "mtime": item["mtime"],
            "active": item["name"] in live,
            "infoHash": ih,
        })
    return out


@router.post("/cache/remove")
def cache_remove(body: RemoveBody, request: Request) -> dict:
    """Delete one cache entry. Guard: must be a plain direct child of cache_root and not a
    protected system file."""
    name = body.name
    if (not name or name in (".", "..") or os.path.basename(name) != name
            or name in cachemod.PROTECTED):
        raise HTTPException(status_code=400, detail="invalid cache entry name")
    root = request.app.state.settings.cache_root
    engine = request.app.state.engine
    ih = _name_to_hash(engine).get(name)
    if ih and engine is not None:
        engine.remove(ih)  # stop libtorrent before deleting its files
    if not ih:
        # The engine knows only the session, and only pinned and wanted torrents are re-added at
        # startup -- so an ordinary cached title has no handle and no entry there. Without the
        # index its partfile would be left behind, which is the part worth deleting.
        ih = cachemod.load_name_index(root).get(name)
    cachemod._remove(os.path.join(root, name))
    # A torrent leaves more than its directory: libtorrent keeps a `.<infohash>.parts` holding file
    # beside the data (one on a real box held 30 GB) and a fast-resume record under `.resume/`.
    # /library/api/remove learned this the expensive way; this route had not.
    if ih and _INFOHASH_RE.match(str(ih)):
        ih = str(ih).lower()
        cachemod._remove(os.path.join(root, f".{ih}.parts"))
        cachemod._remove(os.path.join(root, ".resume", f"{ih}.fastresume"))
    return {"ok": True}
