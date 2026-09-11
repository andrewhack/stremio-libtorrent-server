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
import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi import APIRouter, HTTPException, Request

from stremiosrv import metrics
from stremiosrv.library import addon_model as model
from stremiosrv.library import labels as labelsmod
from stremiosrv.library import netguard
from stremiosrv.library import session as sessionmod
from stremiosrv.library import state as statemod

log = logging.getLogger(__name__)
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
    """Both gates. Raises 404 on failure -- never 401, never a distinguishable message.

    Neither raise passes a `detail`: FastAPI then defaults it to the exact phrase Starlette's own
    router uses for a route that matches nothing ("Not Found"), so the two bodies render
    byte-identical JSON. A hand-written detail string, however similar, is a difference a prober can
    see without ever holding a working token.
    """
    s = _settings(request)
    peer = request.client.host if request.client else ""
    ip = netguard.client_ip(peer, request.headers.get("x-forwarded-for", ""))
    if not netguard.is_allowed(ip, netguard.parse_allow(s.library_addon_allow)):
        raise HTTPException(status_code=404)
    expected = sessionmod.ensure_addon_token(s.cache_root)
    # Bytes, not str: hmac.compare_digest raises TypeError on a str containing non-ASCII
    # characters, and the token comes straight off the URL path -- an uncaught 500 there is just as
    # much a tell as a 401 would be, on a route whose whole point is answering 404 either way.
    if not hmac.compare_digest((token or "").encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=404)


def _origin(request: Request) -> str:
    """The origin the client actually used. Built from the request rather than from a configured
    hostname: the app may reach the box by IP, by name, or through the appliance's own address, and
    a stream URL built from anything else points somewhere the client cannot follow.

    `X-Forwarded-Proto` is trusted on the same condition `netguard.client_ip` trusts
    `X-Forwarded-For`: only when the direct peer is loopback, i.e. the request really arrived
    through this image's own nginx. From any other peer the header is just something the caller
    typed, so the scheme falls back to the connection's own.
    """
    peer = request.client.host if request.client else ""
    if netguard._is_loopback(peer):
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    else:
        proto = request.url.scheme
    # Host has no equivalent fallback and is taken as given regardless of the peer: the client may
    # legitimately reach this server by IP, by hostname, or through the appliance's own address, and
    # a stream URL built from anything other than the host the client actually used would point
    # somewhere it cannot follow.
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
    """`extra` may carry `skip=<n>` (Stremio's paged grid re-requests the same page again once a
    row passes ~100 entries) alongside other `&`-joined pairs, or nothing at all -- Stremio appends
    it whether or not the manifest asks for it. Anything in it besides `skip` is ignored rather than
    failing the row, and a 404 here would empty it with nothing in any log to say why."""
    _guard(request, token)
    if type_ != "other" or catalog_id != model.CATALOG_ID:
        return {"metas": []}
    return {"metas": model.catalog(_state(request), model.parse_skip(extra))}


@router.get("/{token}/meta/{type_}/{meta_id}.json")
def meta(token: str, type_: str, meta_id: str, request: Request) -> dict:
    _guard(request, token)
    parsed = model.parse_id(meta_id)
    if parsed is None:
        # No `detail`: same reason `_guard` passes none -- FastAPI then defaults it to the exact
        # phrase Starlette's own router uses for a route that matches nothing, so every reachable
        # refusal in this file renders the same byte-identical body.
        raise HTTPException(status_code=404)
    entry = model.find_entry(_state(request), parsed[0])
    return {"meta": model.meta_for(entry) if entry else {}}


@router.get("/{token}/stream/{type_}/{stream_id}.json")
def stream(token: str, type_: str, stream_id: str, request: Request) -> dict:
    """Two id shapes: our own (the catalog and its meta pages) and Stremio's `tt…`, which is the
    row that appears in the app's stream list beside every other source.

    The id is parsed BEFORE the library state is built. Stremio asks every installed addon for
    streams on every title the user opens, held or not, so a third id shape -- some other addon's
    own -- arrives here constantly; building state for it would be a full disk scan (state.build
    walks the whole cache directory) with no possible use, once per title, on every page the owner
    opens.
    """
    _guard(request, token)
    parsed = model.parse_id(stream_id)
    if parsed is None and not stream_id.startswith("tt"):
        return {"streams": []}
    state = _state(request)
    origin = _origin(request)
    if parsed is not None:
        ih, idx = parsed
        entry = model.find_entry(state, ih)
        # stream_for yields None for a pack whose files carry no addressable index -- offering
        # nothing is the point of that, so it must not become a [None] here.
        found = model.stream_for(entry, origin, idx) if entry else None
        return {"streams": [found] if found else []}
    return {"streams": model.streams_for_meta_id(state, stream_id, origin)}


def _raw_extra(request: Request, decoded: str) -> str:
    """The `extra` segment exactly as sent, still percent-encoded.

    stremio-core encodes each extra value as a URI component, and the ASGI server decodes the whole
    path before routing -- so by the time the router hands `extra` over, a %26 inside a file name
    has already become a separator. The raw path keeps the encoding; `decoded` is the fallback for
    a server that does not provide one.
    """
    raw = request.scope.get("raw_path") or b""
    last = raw.decode("latin-1").rsplit("/", 1)[-1]
    return last[: -len(".json")] if raw and last.endswith(".json") else decoded


@router.get("/{token}/subtitles/{type_}/{video_id}.json")
@router.get("/{token}/subtitles/{type_}/{video_id}/{extra}.json")
def subtitles(token: str, type_: str, video_id: str, request: Request, extra: str = "") -> dict:
    """Always an empty subtitle list: this resource exists to learn, not to serve.

    The app asks every installed subtitles addon whenever it plays anything, from any addon, and
    says what it is playing: the video id, and the file's size and name. Matched to a torrent on
    disk that has no label, that is enough to label it, and from then on the title's page offers
    the local copy -- see model.learn_labels. A request that cannot teach anything (an id that is
    not `tt…`, no size yet) returns before the state build, which walks the whole cache.
    """
    _guard(request, token)
    report = (model.parse_extra(_raw_extra(request, extra))
              if extra and video_id.startswith("tt") else {})
    if not (report.get("videoSize") or "").isdecimal():
        metrics.record_library_subtitles(reported=False, learned=0)
        return {"subtitles": []}
    cache_root = _settings(request).cache_root
    learned = model.learn_labels(_state(request), type_, video_id, report)
    for ih, label in learned:
        labelsmod.put(cache_root, ih, label)
    metrics.record_library_subtitles(reported=True, learned=len(learned))
    if learned:
        _announce(len(learned))
    return {"subtitles": []}


def _announce(count: int) -> None:
    """Put a count of learned labels in the container log.

    uvicorn surfaces only its own loggers, so without a handler of its own this line never reached
    `docker logs` -- the evictor and the transcoder attach theirs the same way. A count only:
    labels.json is the owner's library and never goes into a log.
    """
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [library] %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    log.info("addon learned the title of %d cached torrent(s) from playback", count)
