"""Optional background tracker-list source.

When STREMIOSRV_TRACKER_LIST_URL is set, this fetches a community tracker list (e.g. the raw
ngosang/trackerslist "best" file) so the announce set stays current without a rebuild. It is
**best-effort and can never block**:

  * The fetch runs in a daemon thread started by `build_app` (like the cache evictor) — nothing on
    the request hot path or the startup path ever awaits it. This is what keeps an offline box from
    hanging at boot (the 0.2.15 lesson): the engine always reads `current()`, an in-memory snapshot,
    and uses whatever is there right now (empty until/unless a fetch has populated it).
  * The fetch itself is bounded by a timeout; on any failure it falls back to the last good value
    (in memory, or the on-disk cache from a previous boot) and simply retries next cycle.
  * When no URL is configured, `start()` is a no-op and no thread is ever created — the code path is
    identical to not having this feature.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request

from stremiosrv.torrent.trackers import parse_tracker_string


def _http_get(url: str, timeout: float) -> str:
    """Fetch `url` and return the body text. Mirrors subs.py's urllib usage (scheme-guarded)."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-http(s) tracker list URL: {url!r}")
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — scheme checked above
        return r.read().decode("utf-8", "replace")


class TrackerSource:
    """Holds the current live tracker list and (optionally) refreshes it in the background."""

    def __init__(
        self,
        url: str,
        cache_path: str | None = None,
        refresh_hours: float = 24.0,
        timeout: float = 10.0,
        fetcher=_http_get,
    ) -> None:
        self._url = url or ""
        self._cache_path = cache_path
        self._interval = max(60.0, refresh_hours * 3600.0)  # never hammer; floor at 1 min
        self._timeout = timeout
        self._fetch = fetcher
        self._lock = threading.Lock()
        self._trackers: list[str] = self._load_cache()

    # ---- read side (hot path): never blocks, never touches the network ----
    def current(self) -> list[str]:
        with self._lock:
            return list(self._trackers)

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    # ---- background refresh ----
    def start(self) -> None:
        """Start the daemon refresh thread. No-op when no URL is configured."""
        if not self._url:
            return
        threading.Thread(target=self._loop, name="tracker-source", daemon=True).start()

    def refresh_once(self) -> bool:
        """Fetch + parse + store once. Returns True if the list was updated. Never raises."""
        try:
            body = self._fetch(self._url, self._timeout)
        except Exception:  # noqa: BLE001 — offline/timeout/HTTP error: keep the last good value
            return False
        parsed = parse_tracker_string(body)
        if not parsed:  # empty or unparseable body: don't clobber a good list
            return False
        with self._lock:
            if parsed == self._trackers:
                return False
            self._trackers = parsed
        self._write_cache(parsed)
        return True

    def _loop(self) -> None:
        while True:
            self.refresh_once()
            time.sleep(self._interval)

    # ---- disk cache (survives reboots so an offline box starts from the last good list) ----
    def _load_cache(self) -> list[str]:
        if not self._cache_path or not os.path.exists(self._cache_path):
            return []
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                return parse_tracker_string(f.read())
        except OSError:
            return []

    def _write_cache(self, trackers: list[str]) -> None:
        if not self._cache_path:
            return
        try:
            tmp = f"{self._cache_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(trackers) + "\n")
            os.replace(tmp, self._cache_path)  # atomic
        except OSError:
            pass  # cache is an optimization; a failed write just means we re-fetch next boot
