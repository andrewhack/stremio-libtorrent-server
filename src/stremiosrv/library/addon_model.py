"""Stremio addon payloads, built from the library state dict.

Pure: every function here takes plain data and returns plain data, so the shapes the app depends on
can be tested without a torrent session, a cache root or a network.
"""
from __future__ import annotations

import re

ADDON_ID = "org.stremiosrv.library"
CATALOG_ID = "library"
CATALOG_NAME = "My Library"
ID_PREFIX = "stremiosrv:"

_IH_RE = re.compile(r"^[0-9a-f]{40}$")


def format_id(info_hash: str, file_idx: int | None = None) -> str:
    """Our meta id for a whole torrent, or for one file inside it."""
    ih = info_hash.lower()
    return f"{ID_PREFIX}{ih}" if file_idx is None else f"{ID_PREFIX}{ih}:{file_idx}"


def parse_id(value: str) -> tuple[str, int | None] | None:
    """(infohash, fileIdx or None), or None when the id is not ours or is malformed.

    This is the validation boundary. The infohash from here is used to look up cache entries and
    ends up in a URL, so it is checked as 40 hex characters and nothing else -- the same guard the
    remove endpoint applies for the same reason.
    """
    if not value.startswith(ID_PREFIX):
        return None
    ih, _, idx = value[len(ID_PREFIX):].partition(":")
    ih = ih.lower()
    if not _IH_RE.match(ih):
        return None
    if not idx:
        return ih, None
    if not idx.isdigit():
        return None
    return ih, int(idx)


def manifest(version: str) -> dict:
    """The addon descriptor.

    One catalog, of type `other`: Stremio catalogs are typed, so a single row holding films, series
    and unlabelled folders together has to be `other` -- which is why the stock local addon uses
    `other` for its own local ids too.
    """
    return {
        "id": ADDON_ID,
        "version": version,
        "name": CATALOG_NAME,
        "description": ("What is on this streaming server: cached titles, kept titles, "
                        "and downloads in progress."),
        "types": ["movie", "series", "other"],
        "catalogs": [{"type": "other", "id": CATALOG_ID, "name": CATALOG_NAME}],
        "resources": [
            {"name": "catalog", "types": ["other"]},
            {"name": "meta", "types": ["other"], "idPrefixes": [ID_PREFIX]},
            {"name": "stream", "types": ["other", "movie", "series"],
             "idPrefixes": [ID_PREFIX, "tt"]},
        ],
        "behaviorHints": {"adult": False, "p2p": False,
                          "configurable": False, "configurationRequired": False},
    }
