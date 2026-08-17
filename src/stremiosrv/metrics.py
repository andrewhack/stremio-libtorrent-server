"""In-process playback metrics for the appliance suggestion advisor.

Counts re-buffer **stalls** (a read that had to wait for a not-yet-downloaded piece), piece
**timeouts** (a piece that never arrived within the read timeout), next-episode **prefetch**
arms (how often the opt-in prefetch fired, and how many bytes it asked for), and **hlsv2 jobs**
split by whether ffmpeg re-encoded anything. Exposed via GET /stats.json and consumed by the
appliance's config-web advisor to suggest raising the download rate limit when playback is starved.
Process-local counters (the server is single-process); reset on restart.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_stalls = 0
_stall_seconds = 0.0
_timeouts = 0
_prefetches = 0
_prefetch_bytes = 0
_subtitle_signature_asks = 0
_hls_sessions = 0
_hls_reencodes = 0


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


def record_subtitle_signature() -> None:
    """A client asked /subtitleSignature (stremio-video >= 0.0.93, once per load whose probe does
    not rule out an embedded subtitle track).

    We answer null because the algorithm is unspecified upstream — see api/subs.py. This counter is
    the evidence for when that changes: it says how often real clients want the feature, which is
    what would justify reverse-engineering a signature rather than guessing one."""
    global _subtitle_signature_asks
    with _lock:
        _subtitle_signature_asks += 1


def record_hls_session(decision: dict) -> None:
    """A NEW hlsv2 job was started. Recorded behind `Converter.ensure_job`'s live-job check, so it
    follows jobs rather than requests — a player re-fetches `master.m3u8` several times per
    playback, and counting the route would inflate this severalfold.

    Two numbers, because they cost wildly different amounts. `decision` comes from
    `transcode.fingerprint.decide()`, which marks each stream `copy` or `transcode`. When *every*
    stream says `copy` the job is a **remux**: the container is rewrapped as HLS and not one frame
    is re-encoded, which is cheap. `hlsReencodes` counts the other kind, where at least one stream
    is genuinely converted — an unsupported codec, too many audio channels, or a frame wider than
    the cap. So `hlsSessions - hlsReencodes` is the remux count.

    The split matters because arriving here is not evidence of a codec problem. A client will
    refuse direct play for reasons that have nothing to do with what the box can serve — notably
    any media carrying an embedded subtitle track — and such a file lands here purely to be
    repackaged. Without the split, those remuxes look identical to real transcode load.
    """
    global _hls_sessions, _hls_reencodes
    reencoded = any(
        isinstance(s, dict) and s.get("action") == "transcode" for s in decision.values()
    )
    with _lock:
        _hls_sessions += 1
        if reencoded:
            _hls_reencodes += 1


def playback_stats() -> dict:
    """Snapshot for /stats.json: cumulative stalls, total stall seconds, timeouts, and how often
    next-episode prefetch armed."""
    with _lock:
        return {
            "stalls": _stalls, "stallSeconds": round(_stall_seconds, 1), "timeouts": _timeouts,
            "prefetches": _prefetches, "prefetchBytes": _prefetch_bytes,
            "subtitleSignatureAsks": _subtitle_signature_asks,
            "hlsSessions": _hls_sessions, "hlsReencodes": _hls_reencodes,
        }


def reset() -> None:
    """Test helper — zero the counters."""
    global _stalls, _stall_seconds, _timeouts, _prefetches, _prefetch_bytes
    global _subtitle_signature_asks, _hls_sessions, _hls_reencodes
    with _lock:
        _stalls, _stall_seconds, _timeouts = 0, 0.0, 0
        _prefetches, _prefetch_bytes = 0, 0
        _subtitle_signature_asks = 0
        _hls_sessions, _hls_reencodes = 0, 0
