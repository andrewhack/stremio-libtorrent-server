import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from stremiosrv.torrent.engine import PinSpaceError

router = APIRouter()

# Pin and unpin are origin-only, but their first path segment is a parameter, so any prefix nginx
# does proxy (/proxy/, /hlsv2/, /library, /subtitles.) would reach them with "proxy", "hlsv2"... as
# the infohash. Only a real infohash reaches the engine (final review of 1.6.7).
_INFO_HASH = re.compile(r"[0-9a-fA-F]{40}")


def _engine(request: Request):
    return getattr(request.app.state, "engine", None)


@router.get("/pins.json")
def pins_list(request: Request) -> list:
    eng = _engine(request)
    return eng.pinned_status() if eng is not None else []


@router.post("/{info_hash}/pin")
def pin(info_hash: str, request: Request):
    if not _INFO_HASH.fullmatch(info_hash):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    eng = _engine(request)
    if eng is None:
        return JSONResponse({"ok": False}, status_code=503)
    try:
        eng.pin(info_hash)
    except PinSpaceError as e:
        return JSONResponse(
            {"error": "insufficient_space", "needed": e.needed, "free": e.free}, status_code=409
        )
    return {"ok": True}


@router.post("/{info_hash}/unpin")
def unpin(info_hash: str, request: Request):
    if not _INFO_HASH.fullmatch(info_hash):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    eng = _engine(request)
    if eng is not None:
        eng.unpin(info_hash)
    return {"ok": True}
