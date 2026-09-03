"""Days-to-expiry for the active TLS cert, for the /health `cert` component.

Shells out to openssl (always present in the runtime image); the result is cached by the cert
file's mtime so the frequent /health poll doesn't spawn openssl every time. Lets us detect, on
time, a lapsing trusted cert — e.g. the shared `*.stremio.rocks` wildcard or a bring-your-own cert.
"""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

_cache: dict[str, tuple[float, int | None]] = {}  # path -> (mtime, days_left|None)


def _parse_enddate(line: str) -> int | None:
    """Parse an openssl `notAfter=...` line into whole days from now (UTC). None if unparseable."""
    try:
        ds = line.strip().split("=", 1)[1]
        exp = datetime.strptime(ds, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    except (IndexError, ValueError):
        return None
    return int((exp - datetime.now(UTC)).total_seconds() // 86400)


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


_san_cache: dict[str, tuple[float, str | None]] = {}  # path -> (mtime, san|None)


def cert_san(path: str) -> str | None:
    """The cert's subjectAltName line, or None if missing/unreadable. Cached by mtime, exactly like
    cert_days_left — the auth path reads this on every sign-in and must not spawn openssl each time.

    Used to tell a bring-your-own cert from the shared `*.stremio.rocks` wildcard, whose private key
    anyone can fetch unauthenticated over plain HTTP (see library/authmode.py).
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _san_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    san: str | None = None
    try:
        out = subprocess.run(
            ["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", path],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            # openssl prints a header line then the indented value; take the value.
            for line in out.stdout.splitlines():
                if "DNS:" in line or "IP Address:" in line:
                    san = line.strip()
                    break
    except (OSError, subprocess.SubprocessError):
        san = None
    _san_cache[path] = (mtime, san)
    return san
