"""Days-to-expiry for the active TLS cert, for the /health `cert` component.

Shells out to openssl (always present in the runtime image); the result is cached by the cert
file's mtime so the frequent /health poll doesn't spawn openssl every time. Lets us detect, on
time, a lapsing trusted cert — e.g. the shared `*.stremio.rocks` wildcard or a bring-your-own cert.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

_cache: dict[str, tuple[float, int | None]] = {}  # path -> (mtime, days_left|None)


def _parse_enddate(line: str) -> int | None:
    """Parse an openssl `notAfter=...` line into whole days from now (UTC). None if unparseable."""
    try:
        ds = line.strip().split("=", 1)[1]
        exp = datetime.strptime(ds, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None
    return int((exp - datetime.now(timezone.utc)).total_seconds() // 86400)


def cert_days_left(path: str) -> int | None:
    """Whole days until the cert at `path` expires; None if missing/unreadable. Cached by mtime."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    days: int | None = None
    try:
        out = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", path],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            days = _parse_enddate(out.stdout)
    except (OSError, subprocess.SubprocessError):
        days = None
    _cache[path] = (mtime, days)
    return days
