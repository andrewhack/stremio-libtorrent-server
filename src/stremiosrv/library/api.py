"""The /library router: an authenticated download manager on the web player's own origin.

Mounted only when `STREMIOSRV_LIBRARY_UI` is set (see app.create_app), so with the flag off none of
these routes exist at all rather than existing and refusing.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(prefix="/library")

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX_HTML, media_type="text/html")
