"""The /library router: an authenticated download manager on the web player's own origin.

Mounted only when `STREMIOSRV_LIBRARY_UI` is set (see app.create_app), so with the flag off none of
these routes exist at all rather than existing and refusing.

Auth is the owner's Stremio account. The browser presents an authKey — read from the web player's
own localStorage on this same origin, or obtained through the sign-in form — and the server proves
it against api.strem.io, checks it against the pinned owner, and issues a session cookie. The
authKey is never stored server-side: stream resolution happens in the browser, so the server has no
use for one.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from stremiosrv import cache as cachemod
from stremiosrv import certcheck
from stremiosrv.library import authmode, stremio_api
from stremiosrv.library import labels as labelsmod
from stremiosrv.library import session as sessionmod
from stremiosrv.library import state as statemod
from stremiosrv.library.ratelimit import RateLimiter
from stremiosrv.torrent.engine import PinSpaceError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/library")

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
COOKIE = "stremiosrv_library"

# 5 attempts / 15 min per source address. Deliberately strict: every attempt is relayed to
# api.strem.io, so this bounds what an internet-facing box can do to somebody else's service.
_login_limiter = RateLimiter(limit=5, window=900)


_INFOHASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class SessionBody(BaseModel):
    authKey: str


class LoginBody(BaseModel):
    email: str
    password: str


class DownloadBody(BaseModel):
    magnet: str
    label: dict | None = None
    # Which file of the torrent was actually chosen. Addons sometimes give one outright; otherwise
    # the label's season/episode identifies it once the file list exists. Without either, a pin
    # means the whole torrent -- which for a season pack is tens of gigabytes for one episode.
    fileIdx: int | None = None


class RemoveBody(BaseModel):
    infoHash: str


def _settings(request: Request):
    return request.app.state.settings


def _cert_path(settings) -> str:
    return os.path.join(settings.cache_root, settings.cert_file)


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto")
    return (proto or request.url.scheme) == "https"


def _require_tls(request: Request) -> None:
    """The session cookie carries `Secure`, and the sign-in form puts a password on the wire. Refuse
    plain HTTP unless the operator has said, explicitly, that they are behind a VPN or trusted LAN.
    """
    s = _settings(request)
    if not _is_https(request) and not s.library_allow_http:
        raise HTTPException(
            status_code=400,
            detail="the library UI requires HTTPS; set STREMIOSRV_LIBRARY_ALLOW_HTTP=true "
                   "only on a trusted network",
        )


def _require_same_origin(request: Request) -> None:
    """Reject a state-changing request whose Origin (or, failing that, Referer) is present and does
    not match the Host. Same-origin calls from our own page match; non-browser clients send no
    Origin and are allowed; only a cross-origin browser POST — the CSRF vector — is blocked. Same
    guard config-web already runs, and it matters more here: this origin faces the internet and the
    session is a cookie.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    source = request.headers.get("origin") or request.headers.get("referer")
    if source and urlparse(source).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


def _password_login_allowed(request: Request) -> bool:
    return authmode.password_login_allowed(certcheck.cert_san(_cert_path(_settings(request))))


def _set_cookie(request: Request, response: Response, sid: str) -> None:
    response.set_cookie(
        COOKIE, sid, httponly=True, samesite="lax",
        secure=_is_https(request), max_age=sessionmod.SESSION_TTL, path="/library",
    )


def require_session(request: Request) -> None:
    """Dependency for every non-auth route."""
    _require_tls(request)
    _require_same_origin(request)
    if not sessionmod.verify_session(_settings(request).cache_root,
                                     request.cookies.get(COOKIE, "")):
        raise HTTPException(status_code=401, detail="not signed in")


def _authorise(request: Request, user: dict, response: Response) -> None:
    """Common tail of both sign-in paths: pin/verify the owner, then mint a session."""
    s = _settings(request)
    try:
        sessionmod.claim_owner(s.cache_root, user, s.library_owner)
    except sessionmod.OwnerMismatch:
        log.warning("library: sign-in refused - not this server's owner")
        raise HTTPException(status_code=403, detail="not this server's owner") from None
    _set_cookie(request, response, sessionmod.new_session(s.cache_root))


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The page, explicitly uncacheable.

    Served without these, a browser holds the previous build and every redeploy becomes a question
    of whether the person testing is even running the new code. That turned caching into an
    uncontrolled variable across several debugging rounds, which is worse than the bugs it hid.
    """
    return FileResponse(
        INDEX_HTML, media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/api/config")
def config(request: Request) -> dict:
    """What the page needs before anyone is signed in: whether to render the password form at all."""
    _require_tls(request)
    san = certcheck.cert_san(_cert_path(_settings(request)))
    return {"passwordLogin": authmode.password_login_allowed(san),
            "certShared": authmode.is_shared_cert(san)}


@router.post("/api/session")
def create_session(body: SessionBody, request: Request, response: Response) -> dict:
    """Exchange an authKey the browser already had for a session cookie."""
    _require_tls(request)
    _require_same_origin(request)
    try:
        user = stremio_api.get_user(body.authKey)
    except stremio_api.StremioApiError:
        # Deliberately not echoing the upstream message: it distinguishes "no such session" from
        # other failures, which is a probing aid and tells the caller nothing they can act on.
        raise HTTPException(status_code=401, detail="invalid Stremio session") from None
    _authorise(request, user, response)
    return {"user": {"_id": user.get("_id"), "email": user.get("email")}}


@router.post("/api/login")
def password_login(body: LoginBody, request: Request, response: Response) -> dict:
    """Sign-in for a browser with no Stremio session on this origin.

    The password is relayed to api.strem.io and discarded — never stored, never logged. The returned
    authKey goes back to the page, which needs one to call the owner's addons.
    """
    _require_tls(request)
    _require_same_origin(request)
    # Before the relay, not after: a 409 issued once the password had already been forwarded would
    # leak it to the very network this gate exists to protect it from.
    if not _password_login_allowed(request):
        raise HTTPException(
            status_code=409,
            detail="this server uses the shared stremio.rocks certificate, whose private key is "
                   "public; sign in from a browser that already has your Stremio session, or "
                   "install your own certificate",
        )
    client = request.client.host if request.client else "unknown"
    if not _login_limiter.allow(client):
        raise HTTPException(status_code=429, detail="too many sign-in attempts")
    try:
        result = stremio_api.login(body.email, body.password)
    except stremio_api.StremioApiError:
        # One opaque answer for every failure. api.strem.io distinguishes a wrong email from a wrong
        # password (`wrongEmail: true`); re-exporting that would make this box an
        # account-enumeration oracle for someone else's user base.
        raise HTTPException(status_code=401, detail="sign-in failed") from None
    auth_key = result.get("authKey") or ""
    user = result.get("user") or {}
    if not user.get("_id"):
        user = stremio_api.get_user(auth_key)
    _authorise(request, user, response)
    return {"authKey": auth_key,
            "user": {"_id": user.get("_id"), "email": user.get("email")}}


@router.delete("/api/session")
def destroy_session(request: Request, response: Response) -> dict:
    _require_tls(request)
    _require_same_origin(request)
    sessionmod.drop_session(_settings(request).cache_root, request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/library")
    return {"ok": True}


@router.get("/api/state", dependencies=[Depends(require_session)])
def state(request: Request) -> dict:
    s = _settings(request)
    return statemod.build(s.cache_root, request.app.state.engine, budget=int(s.cache_size))


def _engine_or_503(request: Request):
    eng = request.app.state.engine
    if eng is None:
        raise HTTPException(status_code=503, detail="torrent engine unavailable")
    return eng


def _wanted_file(body: DownloadBody) -> dict | None:
    """What of the torrent this download actually asked for, or None for all of it.

    An explicit index from the addon wins; otherwise the label already carries the season and
    episode the owner clicked, and that is enough to pick the file out of a pack once metadata
    arrives. A film, or a series label without both numbers, keeps the old whole-torrent meaning.
    """
    if body.fileIdx is not None:
        return {"fileIdx": body.fileIdx}
    label = body.label or {}
    season, episode = label.get("season"), label.get("episode")
    if season is None or episode is None:
        return None
    return {"season": season, "episode": episode}


@router.post("/api/download", dependencies=[Depends(require_session)])
def download(body: DownloadBody, request: Request) -> dict:
    """Start a full download of a magnet the PAGE resolved.

    The server does not talk to addons — stream lookup happens in the browser, so what arrives here
    is a magnet, exactly as on the streaming path. Add first, then pin: `Engine.pin` can re-add from
    a resume file, but a title this box has never seen has neither a handle nor a resume file.
    """
    if not body.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="a magnet URI is required")
    eng = _engine_or_503(request)
    try:
        handle = eng.add(body.magnet)
    except Exception as e:  # noqa: BLE001 - a magnet libtorrent cannot parse is a bad request
        log.warning("library: add failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=400, detail="could not parse that magnet") from None
    info_hash = handle.info_hash().lower()
    # Wanted, not pinned. A download is the same operation as playback -- "this file is wanted" --
    # and pinning it would hand it an eviction exemption nobody asked for, which is how a season
    # pack came to sit far above the cache budget with the evictor unable to touch it. Keeping a
    # title is a separate, manual act, and there is no disk guard here for the same reason there is
    # none on playback: the evictor is what manages space.
    eng.want(info_hash, _wanted_file(body))
    if body.label:
        labelsmod.put(_settings(request).cache_root, info_hash, body.label)
    return {"ok": True, "infoHash": info_hash}


@router.post("/api/pin", dependencies=[Depends(require_session)])
def pin(body: RemoveBody, request: Request) -> dict:
    """Keep this title: exempt it from eviction, whole, and seed it.

    Deliberately the same act as the appliance's own pin control, calling the same engine method --
    a title is kept or it is not, and two surfaces that mean different things by "pinned" would be
    worse than either. This is the ONLY way anything becomes pinned: downloading does not do it on
    your behalf, so a download stays ordinary cache the evictor may reclaim, which is what makes
    the budget mean anything.

    The disk guard belongs here rather than on the download, because this is the promise that can
    overrun a disk: a pin the evictor may not touch.
    """
    if not _INFOHASH_RE.match(body.infoHash or ""):
        raise HTTPException(status_code=400, detail="invalid infohash")
    eng = _engine_or_503(request)
    try:
        eng.pin(body.infoHash.lower())
    except PinSpaceError as e:
        # Flat body, matching /{ih}/pin: two spellings of one error is how they drift apart.
        raise HTTPException(
            status_code=409,
            detail={"error": "insufficient_space", "needed": e.needed, "free": e.free},
        ) from None
    return {"ok": True}


@router.post("/api/unpin", dependencies=[Depends(require_session)])
def unpin(body: RemoveBody, request: Request) -> dict:
    """Stop keeping this title. It stays on disk and stays playable -- it simply becomes ordinary
    cache again, which the evictor may reclaim. Distinct from Remove, which deletes it now."""
    if not _INFOHASH_RE.match(body.infoHash or ""):
        raise HTTPException(status_code=400, detail="invalid infohash")
    eng = _engine_or_503(request)
    eng.unpin(body.infoHash.lower())
    return {"ok": True}


@router.post("/api/remove", dependencies=[Depends(require_session)])
def remove(body: RemoveBody, request: Request) -> dict:
    """Unpin, stop the torrent, delete everything it left on disk, forget its label.

    A torrent leaves more than its directory behind. libtorrent keeps a `.<infohash>.parts`
    holding file beside the data and a fast-resume record under `.resume/`; deleting only the
    directory left both, and the partfile is not small -- one on a real box held 30 GB after a
    pinned download, so "Remove" reclaimed almost nothing and the disk stayed full.
    """
    if not _INFOHASH_RE.match(body.infoHash or ""):
        raise HTTPException(status_code=400, detail="invalid infohash")
    info_hash = body.infoHash.lower()
    s = _settings(request)
    eng = _engine_or_503(request)
    names = {h.lower(): n for n, h in (eng.name_to_hash() or {}).items()}
    eng.unpin(info_hash)
    eng.unwant(info_hash)  # forget the selectors too, or a restart resumes what was just removed
    eng.remove(info_hash)  # drops it from the session: downloading stops before anything is deleted
    name = names.get(info_hash)
    # The name comes from the TORRENT, not from the operator. Require a plain direct child of
    # cache_root and refuse PROTECTED names, so a torrent called `../../something` or `pins.json`
    # cannot steer the delete. Same guard /cache/remove already applies for the same reason.
    if name and os.path.basename(name) == name and name not in cachemod.PROTECTED:
        cachemod._remove(os.path.join(s.cache_root, name))
    # Both of these are named from the infohash, which is already validated as 40 hex above, so
    # neither can be steered anywhere.
    cachemod._remove(os.path.join(s.cache_root, f".{info_hash}.parts"))
    cachemod._remove(os.path.join(s.cache_root, ".resume", f"{info_hash}.fastresume"))
    labelsmod.drop(s.cache_root, info_hash)
    return {"ok": True}
