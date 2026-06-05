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
    "udp://open.stealth.si:80/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://tracker.0x7c0.com:6969/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://explodie.org:6969/announce",
    "udp://uploads.gamecoast.net:6969/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
    "udp://opentracker.io:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://retracker.lanta-net.ru:2710/announce",
    "https://tracker.tamersunion.org:443/announce",
    "https://tracker.gbitt.info:443/announce",
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
