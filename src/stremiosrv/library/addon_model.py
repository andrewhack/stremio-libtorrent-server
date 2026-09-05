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
    # isdecimal() matches what int() will accept (0-9 only), unlike isdigit() which is true for
    # Unicode digits like ² and ③ that cause int() to raise ValueError. The contract is to return
    # None for malformed input, not raise, so the boundary rejects anything int() won't decode.
    if not idx.isdecimal():
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


def human_size(n: int) -> str:
    """Sizes for a description line. GB throughout rather than a scaling unit: a catalog row is
    scanned, not read, and one unit keeps two rows comparable at a glance."""
    return f"{(n or 0) / 1073741824:.2f} GB"


def display_name(entry: dict) -> str:
    label = entry.get("label") or {}
    name = label.get("name") or entry.get("name") or ""
    if entry.get("state") == "downloading":
        return f"{name} · {round((entry.get('progress') or 0) * 100)}%"
    return name


def describe(entry: dict) -> str:
    bits = [human_size(entry.get("size") or 0)]
    state = entry.get("state") or "idle"
    bits.append("downloading" if state == "downloading" else
                ("seeding" if state == "seeding" else "on disk"))
    if entry.get("pinned"):
        bits.append("kept")
    if entry.get("seeds"):
        bits.append(f"{entry['seeds']} seeders")
    return " · ".join(bits)


def is_title(entry: dict) -> bool:
    """Something that can actually be played. Orphan partfiles are real disk usage with no torrent
    and no file, and an entry with no infohash cannot be addressed at all."""
    return entry.get("kind") != "orphan" and bool(entry.get("infoHash"))


def preview(entry: dict) -> dict:
    label = entry.get("label") or {}
    item = {
        "id": format_id(entry["infoHash"]),
        "type": "other",
        "name": display_name(entry),
        "description": describe(entry),
    }
    if label.get("poster"):
        item["poster"] = label["poster"]
    return item


def catalog(state: dict) -> list[dict]:
    return [preview(e) for e in state.get("entries", []) if is_title(e)]


def playable_index(entry: dict) -> int:
    """Which file in the torrent this entry means.

    The recorded selection wins -- that is the file the download was started for. Otherwise the
    largest file on disk, because a pack's feature is its big file and its samples are not. With no
    file list at all (an entry with no engine record and nothing readable on disk) index 0 is the
    only answer available, and for a single-file torrent it is also the right one.
    """
    wanted = entry.get("wantedFile")
    if isinstance(wanted, int):
        return wanted
    files = entry.get("files") or []
    if not files:
        return 0
    return max(files, key=lambda f: f.get("size") or 0).get("index") or 0


def stream_for(entry: dict, origin: str, file_idx: int | None = None) -> dict:
    """One stream entry pointing at the copy already on disk.

    `bingeGroup` ties every episode of one torrent together so the app can play the next one
    without asking again.
    """
    ih = entry["infoHash"].lower()
    idx = playable_index(entry) if file_idx is None else file_idx
    return {
        "url": f"{origin}/{ih}/{idx}",
        "name": CATALOG_NAME,
        "title": describe(entry),
        "behaviorHints": {"bingeGroup": f"{ID_PREFIX}{ih}"},
    }


def _label_matches(label: dict, base: str, season: int | None, episode: int | None) -> bool:
    if (label.get("metaId") or "") != base:
        return False
    if season is None:
        return label.get("season") is None and label.get("episode") is None
    return label.get("season") == season and label.get("episode") == episode


def streams_for_meta_id(state: dict, meta_id: str, origin: str) -> list[dict]:
    """Streams for a Stremio meta id (`tt…` or `tt…:S:E`).

    Matching is on the label, which is the only place this server records what a torrent IS. An
    entry with no label cannot match and must not: guessing an identity from a folder name would
    put the wrong film behind a right-looking row.
    """
    parts = meta_id.split(":")
    base = parts[0]
    season = int(parts[1]) if len(parts) > 2 and parts[1].isdecimal() else None
    episode = int(parts[2]) if len(parts) > 2 and parts[2].isdecimal() else None
    out: list[dict] = []
    for e in state.get("entries", []):
        if not is_title(e):
            continue
        label = e.get("label") or {}
        if label and _label_matches(label, base, season, episode):
            out.append(stream_for(e, origin))
    return out
