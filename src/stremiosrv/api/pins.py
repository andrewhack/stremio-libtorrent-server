from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from stremiosrv.torrent.engine import PinSpaceError

router = APIRouter()


def _engine(request: Request):
    return getattr(request.app.state, "engine", None)


@router.get("/pins.json")
def pins_list(request: Request) -> list:
    eng = _engine(request)
    return eng.pinned_status() if eng is not None else []


@router.post("/{info_hash}/pin")
def pin(info_hash: str, request: Request):
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
    eng = _engine(request)
    if eng is not None:
        eng.unpin(info_hash)
    return {"ok": True}
