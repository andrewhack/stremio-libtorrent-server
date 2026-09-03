"""Fixed-window rate limiter for the sign-in endpoint.

That endpoint relays credentials to api.strem.io. Unlimited, an internet-facing box becomes a
credential-stuffing proxy against somebody else's service — so the cap protects Stremio as much as
it protects the owner.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Thread-safe. uvicorn runs sync route handlers in a threadpool, so `allow` really is called
    concurrently, and read-count-then-append is a check-then-act race: two threads can both see
    len(hits) < limit and both append.

    Measured, not assumed — the unlocked version granted **14** of 60 concurrent attempts against a
    limit of 5 once `sys.setswitchinterval` was lowered to force preemption. At the default 5 ms
    interval the GIL hides it, which is precisely what makes it worth a lock rather than a comment:
    it would surface under real load, on a faster machine, or on a free-threaded build, on the one
    endpoint that relays credentials to somebody else's service.
    """

    def __init__(self, limit: int, window: int) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Record an attempt for `key` and say whether it is within the cap."""
        now = time.time() if now is None else now
        cutoff = now - self.window
        with self._lock:
            # Reap every stale key, not just this one: the table is keyed by source address and this
            # is reachable from the internet, so pruning lazily per-key would let a flood of
            # one-shot addresses grow it without bound.
            for k in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
                del self._hits[k]
            hits = [t for t in self._hits.get(key, []) if t > cutoff]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True
