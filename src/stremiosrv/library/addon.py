"""The Stremio addon surface over the library.

Read-only by protocol: catalog, meta and stream are resources, not actions. Managing the library
(keep, remove, start a download) stays on the page, which is the only place that can express it.

Two gates, both answering 404 rather than 401, because a 401 confirms the route exists:
  * a token in the path -- the app fetches these URLs itself, so there is no session cookie;
  * the client's address must be on a private network -- the install URL syncs into the owner's
    Stremio account, so the token alone is not a boundary.
"""
from __future__ import annotations

import hmac
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi import APIRouter, HTTPException, Request

from stremiosrv.library import addon_model as model
from stremiosrv.library import netguard
from stremiosrv.library import session as sessionmod
from stremiosrv.library import state as statemod

router = APIRouter(prefix="/library/addon")

# Same source health.py uses. There is no `version` on Settings, and hardcoding one here would be
# a second place to forget on a release.
try:
    _VERSION = _pkg_version("stremiosrv")
except PackageNotFoundError:  # pragma: no cover - only when not installed as a package
    _VERSION = "0.0.0"


def _settings(request: Request):
    return request.app.state.settings


def _guard(request: Request, token: str) -> None:
    """Both gates. Raises 404 on failure -- never 401, never a distinguishable message."""
    s = _settings(request)
    peer = request.client.host if request.client else ""
    ip = netguard.client_ip(peer, request.headers.get("x-forwarded-for", ""))
    if not netguard.is_allowed(ip, netguard.parse_allow(s.library_addon_allow)):
        raise HTTPException(status_code=404, detail="not found")
    expected = sessionmod.ensure_addon_token(s.cache_root)
    if not hmac.compare_digest(token or "", expected):
        raise HTTPException(status_code=404, detail="not found")


def _origin(request: Request) -> str:
    """The origin the client actually used. Built from the request rather than from a configured
    hostname: the app may reach the box by IP, by name, or through the appliance's own address, and
    a stream URL built from anything else points somewhere the client cannot follow."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _state(request: Request) -> dict:
    s = _settings(request)
    return statemod.build(s.cache_root, request.app.state.engine, budget=int(s.cache_size))


@router.get("/{token}/manifest.json")
def manifest(token: str, request: Request) -> dict:
    _guard(request, token)
    return model.manifest(_VERSION)


@router.get("/{token}/catalog/{type_}/{catalog_id}.json")
@router.get("/{token}/catalog/{type_}/{catalog_id}/{extra}.json")
def catalog(token: str, type_: str, catalog_id: str, request: Request, extra: str = "") -> dict:
    """`extra` is accepted and ignored. Stremio appends it (skip=…) whether or not the manifest
    asks for it, and a 404 there empties the row with nothing in any log to say why."""
    _guard(request, token)
    if type_ != "other" or catalog_id != model.CATALOG_ID:
        return {"metas": []}
    return {"metas": model.catalog(_state(request))}


@router.get("/{token}/meta/{type_}/{meta_id}.json")
def meta(token: str, type_: str, meta_id: str, request: Request) -> dict:
    _guard(request, token)
    parsed = model.parse_id(meta_id)
    if parsed is None:
        raise HTTPException(status_code=404, detail="not found")
    entry = model.find_entry(_state(request), parsed[0])
    return {"meta": model.meta_for(entry) if entry else {}}


@router.get("/{token}/stream/{type_}/{stream_id}.json")
def stream(token: str, type_: str, stream_id: str, request: Request) -> dict:
    """Two id shapes: our own (the catalog and its meta pages) and Stremio's `tt…`, which is the
    row that appears in the app's stream list beside every other source."""
    _guard(request, token)
    state = _state(request)
    origin = _origin(request)
    parsed = model.parse_id(stream_id)
    if parsed is not None:
        ih, idx = parsed
        entry = model.find_entry(state, ih)
        # stream_for yields None for a pack whose files carry no addressable index -- offering
        # nothing is the point of that, so it must not become a [None] here.
        found = model.stream_for(entry, origin, idx) if entry else None
        return {"streams": [found] if found else []}
    if stream_id.startswith("tt"):
        return {"streams": model.streams_for_meta_id(state, stream_id, origin)}
    return {"streams": []}
