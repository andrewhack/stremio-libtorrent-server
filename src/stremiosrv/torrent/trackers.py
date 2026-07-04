"""Default public BitTorrent trackers + tracker-string parsing + merge logic.

The stock Stremio server seeds every torrent with a curated tracker list (plus DHT) so peer
discovery works from a bare infohash. We mirror that, and extend it: operators can add their own
trackers via STREMIOSRV_EXTRA_TRACKERS, and an optional background source
(STREMIOSRV_TRACKER_LIST_URL) can keep the list current. This module stays import-light (no
libtorrent, no network) so the parse/merge logic is unit-testable anywhere.
"""
from __future__ import annotations

import re

# Curated union of long-stable public trackers plus the stable subset of the community-maintained
# ngosang/trackerslist "best" list (snapshot 2026-07-04). This is only the baked-in baseline; when
# STREMIOSRV_TRACKER_LIST_URL is set, tracker_source.TrackerSource keeps an up-to-date list on top.
DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://tracker2.dler.org:80/announce",
    "udp://tracker.0x7c0.com:6969/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://explodie.org:6969/announce",
    "udp://uploads.gamecoast.net:6969/announce",
    "udp://opentracker.io:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://tracker.auctor.tv:6969/announce",
    "udp://t.overflow.biz:6969/announce",
    "udp://open.demonoid.ch:6969/announce",
    "udp://leet-tracker.moe:1337/announce",
    # Parity with the stock Stremio server's default set (the entries it ships that we lacked).
    "udp://bt.rer.lol:6969/announce",
    "udp://open.dstud.io:6969/announce",
    "udp://run.publictracker.xyz:6969/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://tracker.therarbg.to:6969/announce",
    "udp://tracker1.myporn.club:9337/announce",
    "https://tracker.tamersunion.org:443/announce",
    "https://tracker.gbitt.info:443/announce",
]

# Accepted tracker URL schemes (BitTorrent tracker + WebTorrent). Anything else (comments, junk,
# magnet fragments) is rejected by the parser.
_TRACKER_SCHEMES = ("udp://", "http://", "https://", "ws://", "wss://")
_SPLIT = re.compile(r"[\s,]+")


def is_tracker_url(url: str) -> bool:
    """True if `url` looks like a tracker announce URL we're willing to add."""
    return isinstance(url, str) and url.startswith(_TRACKER_SCHEMES)


def parse_tracker_string(raw: str | None) -> list[str]:
    """Parse trackers from a free-form string (env value or a fetched list body).

    Splits on any run of whitespace/commas, keeps only valid tracker URLs, de-dupes preserving
    order. Handles the STREMIOSRV_EXTRA_TRACKERS env format AND newline-separated remote lists
    (comment/blank lines fail the scheme check and are dropped).
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in _SPLIT.split(raw.strip()):
        if is_tracker_url(tok) and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def merge_trackers(
    existing: list[str] | None = None,
    extra: list[str] | None = None,
    *,
    env: list[str] | None = None,
    live: list[str] | None = None,
) -> list[str]:
    """Build one torrent's announce list, de-duped and order-preserving.

    Priority order (earlier wins on duplicates): existing (from the magnet/resume) -> built-in
    DEFAULT_TRACKERS -> operator env extras -> live-fetched list -> client-supplied extra.
    """
    out: list[str] = []
    seen: set[str] = set()
    for group in (existing, DEFAULT_TRACKERS, env, live, extra):
        for t in group or []:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out
