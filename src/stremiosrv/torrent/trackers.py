"""Default public BitTorrent trackers + merge logic.

The stock Stremio server seeds every torrent with a curated tracker list (plus DHT) so peer
discovery works from a bare infohash. We mirror that. Kept in its own module (no libtorrent import)
so the merge logic is unit-testable anywhere.
"""
from __future__ import annotations

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.demonii.com:1337/announce",
]


def merge_trackers(existing: list[str] | None = None, extra: list[str] | None = None) -> list[str]:
    """existing (e.g. from a magnet) + defaults + client-supplied extra, de-duped, order-preserving."""
    out: list[str] = []
    seen: set[str] = set()
    for t in list(existing or []) + DEFAULT_TRACKERS + list(extra or []):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out
