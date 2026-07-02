"""Seeding policy (stop-on-complete / max-seed-time) + cross-torrent active prioritization.

Pure decision helpers are unit-tested here; the Engine loop/threads that call them need libtorrent
and are covered by integration tests.
"""
from __future__ import annotations

from stremiosrv.torrent.engine import Handle, idle_download_limit, should_stop_seeding


# ---- idle_download_limit: cross-torrent active prioritization ----
def test_idle_limit_caps_only_idle_while_something_plays() -> None:
    limit = 1_000_000
    assert idle_download_limit(this_active=False, any_active=True, idle_limit=limit) == limit  # idle -> capped
    assert idle_download_limit(this_active=True, any_active=True, idle_limit=limit) == 0        # the player -> uncapped
    assert idle_download_limit(this_active=False, any_active=False, idle_limit=limit) == 0      # nothing playing -> uncapped
    assert idle_download_limit(this_active=False, any_active=True, idle_limit=0) == 0           # feature off


# ---- should_stop_seeding ----
def test_seed_forever_by_default() -> None:
    assert should_stop_seeding(pinned=False, seeding=True, completed_at=0.0, now=10_000,
                               seed_on_complete=True, max_seed_minutes=0) is False


def test_stop_immediately_when_seed_on_complete_disabled() -> None:
    assert should_stop_seeding(pinned=False, seeding=True, completed_at=100.0, now=100.0,
                               seed_on_complete=False, max_seed_minutes=0) is True


def test_pinned_always_keeps_seeding() -> None:
    assert should_stop_seeding(pinned=True, seeding=True, completed_at=0.0, now=10_000,
                               seed_on_complete=False, max_seed_minutes=1) is False


def test_incomplete_torrent_never_stops() -> None:
    assert should_stop_seeding(pinned=False, seeding=False, completed_at=None, now=10_000,
                               seed_on_complete=False, max_seed_minutes=0) is False


def test_max_seed_minutes_boundary() -> None:
    # 10-minute policy: stop once >= 600s since completion, not before.
    assert should_stop_seeding(pinned=False, seeding=True, completed_at=0.0, now=601,
                               seed_on_complete=True, max_seed_minutes=10) is True
    assert should_stop_seeding(pinned=False, seeding=True, completed_at=0.0, now=599,
                               seed_on_complete=True, max_seed_minutes=10) is False


# ---- Handle control methods via a fake libtorrent handle ----
class _St:
    def __init__(self, seeding: bool) -> None:
        self.is_seeding = seeding


class _FakeH:
    def __init__(self, seeding: bool = False) -> None:
        self._seeding = seeding
        self.paused = False
        self.dl_limit: int | None = None

    def status(self) -> _St:
        return _St(self._seeding)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def set_download_limit(self, n: int) -> None:
        self.dl_limit = n


def test_handle_seeding_pause_resume() -> None:
    fake = _FakeH(seeding=True)
    h = Handle(fake)
    assert h.is_seeding() is True
    assert h.is_paused() is False
    h.pause()
    assert fake.paused is True and h.is_paused() is True
    h.resume()
    assert fake.paused is False and h.is_paused() is False


def test_handle_set_download_limit() -> None:
    fake = _FakeH()
    h = Handle(fake)
    h.set_download_limit(500_000)
    assert fake.dl_limit == 500_000
