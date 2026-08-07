"""In-process playback metrics for the appliance suggestion advisor.

Counts re-buffer **stalls** (a read that had to wait for a not-yet-downloaded piece), piece
**timeouts** (a piece that never arrived within the read timeout), and next-episode **prefetch**
arms (how often the opt-in prefetch fired, and how many bytes it asked for). Exposed via
GET /stats.json and consumed by the appliance's config-web advisor to suggest raising the download
rate limit when playback is starved. Process-local counters (the server is single-process); reset on
restart.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_stalls = 0
_stall_seconds = 0.0
_timeouts = 0
_prefetches = 0
_prefetch_bytes = 0


def record_stall(seconds: float) -> None:
    """A read waited `seconds` for the covering piece to arrive (a playback re-buffer)."""
    global _stalls, _stall_seconds
    with _lock:
        _stalls += 1
        _stall_seconds += seconds


def record_timeout() -> None:
    """A piece never arrived within the read timeout (hard stall / failed read)."""
    global _timeouts
    with _lock:
        _timeouts += 1


def record_prefetch(planned_bytes: int) -> None:
    """A next-episode head was armed. `planned_bytes` is what was REQUESTED, not what arrived —
    the counter exists to answer 'did the opt-in feature fire, and how much did it ask for' on a
    real box before the default is ever flipped."""
    global _prefetches, _prefetch_bytes
    with _lock:
        _prefetches += 1
        _prefetch_bytes += planned_bytes


def playback_stats() -> dict:
    """Snapshot for /stats.json: cumulative stalls, total stall seconds, timeouts, and how often
    next-episode prefetch armed."""
    with _lock:
        return {
            "stalls": _stalls, "stallSeconds": round(_stall_seconds, 1), "timeouts": _timeouts,
            "prefetches": _prefetches, "prefetchBytes": _prefetch_bytes,
        }


def reset() -> None:
    """Test helper — zero the counters."""
    global _stalls, _stall_seconds, _timeouts, _prefetches, _prefetch_bytes
    with _lock:
        _stalls, _stall_seconds, _timeouts = 0, 0.0, 0
        _prefetches, _prefetch_bytes = 0, 0
