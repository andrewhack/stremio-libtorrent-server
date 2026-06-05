"""Casting API. Parity stub: clients query /casting for DLNA renderers; we expose an empty list
(no discovery yet) so clients get a valid response instead of a 404."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/casting")
def casting() -> list:
    return []
