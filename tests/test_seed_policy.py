"""Seeding policy (stop-on-complete / max-seed-time) + cross-torrent active prioritization.

Pure decision helpers are unit-tested here; the Engine loop/threads that call them need libtorrent
and are covered by integration tests.
"""
from __future__ import annotations

from stremiosrv.torrent.engine import (
    Handle,
    idle_download_limit,
    should_resume_on_open,
    should_stop_seeding,
)


# ---- idle_download_limit: cross-torrent active prioritization ----
def test_idle_limit_caps_only_idle_while_something_plays() -> None:
    limit = 1_000_000
    assert idle_download_limit(this_active=False, any_active=True, idle_limit=limit) == limit  # idle -> capped
    assert idle_download_limit(this_active=True, any_active=True, idle_limit=limit) == 0        # the player -> uncapped
    assert idle_download_limit(this_active=False, any_active=False, idle_limit=limit) == 0      # nothing playing -> uncapped
    assert idle_download_limit(this_active=False, any_active=True, idle_limit=0) == 0           # feature off


# ---- should_stop_seeding ----
def test_seed_forever_by_default() -> None:
    assert should_stop_seeding(pinned=False, finished=True, completed_at=0.0, now=10_000,
                               seed_on_complete=True, max_seed_minutes=0) is False


def test_stop_immediately_when_seed_on_complete_disabled() -> None:
    assert should_stop_seeding(pinned=False, finished=True, completed_at=100.0, now=100.0,
                               seed_on_complete=False, max_seed_minutes=0) is True


def test_pinned_always_keeps_seeding() -> None:
    assert should_stop_seeding(pinned=True, finished=True, completed_at=0.0, now=10_000,
                               seed_on_complete=False, max_seed_minutes=1) is False


def test_incomplete_torrent_never_stops() -> None:
    assert should_stop_seeding(pinned=False, finished=False, completed_at=None, now=10_000,
                               seed_on_complete=False, max_seed_minutes=0) is False


def test_max_seed_minutes_boundary() -> None:
    # 10-minute policy: stop once >= 600s since completion, not before.
    assert should_stop_seeding(pinned=False, finished=True, completed_at=0.0, now=601,
                               seed_on_complete=True, max_seed_minutes=10) is True
    assert should_stop_seeding(pinned=False, finished=True, completed_at=0.0, now=599,
                               seed_on_complete=True, max_seed_minutes=10) is False


# ---- should_resume_on_open: resume a seed-paused torrent when a new file needs downloading ----
def test_resume_on_open_only_when_paused_and_unfinished() -> None:
    # Next episode of a stop-seed-paused pack (paused + not finished) -> resume so it can download.
    assert should_resume_on_open(paused=True, finished=False) is True
    # Re-watching a finished torrent that was paused -> stay paused, play from disk (no re-seed).
    assert should_resume_on_open(paused=True, finished=True) is False
    # Not paused -> nothing to do (already running).
    assert should_resume_on_open(paused=False, finished=False) is False
    assert should_resume_on_open(paused=False, finished=True) is False


# ---- Handle control methods via a fake libtorrent handle ----
class _St:
    def __init__(self, seeding: bool, finished: bool | None = None) -> None:
        self.is_seeding = seeding
        # A full seed is also finished; a partially-watched pack can be finished but NOT seeding.
        self.is_finished = seeding if finished is None else finished


class _FakeH:
    def __init__(self, seeding: bool = False, finished: bool | None = None) -> None:
        self._seeding = seeding
        self._finished = seeding if finished is None else finished
        self.paused = False
        self.dl_limit: int | None = None
        self.cleared_flags: list = []  # records unset_flags() args
        self.set_flags_calls: list = []

    def status(self) -> _St:
        return _St(self._seeding, self._finished)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def set_download_limit(self, n: int) -> None:
        self.dl_limit = n

    def unset_flags(self, f) -> None:
        self.cleared_flags.append(f)

    def set_flags(self, f) -> None:
        self.set_flags_calls.append(f)


def test_finished_pack_stops_even_when_not_a_full_seed() -> None:
    """The TV-pack bug: a partially-watched multi-file torrent is is_finished (all wanted data) +
    progress 1.0 but NOT is_seeding (un-watched episodes are priority 0). The policy must stop it —
    it keys off `finished`, so finished=True + seed_on_complete=False -> stop, regardless of seeding."""
    assert should_stop_seeding(pinned=False, finished=True, completed_at=100.0, now=100.0,
                               seed_on_complete=False, max_seed_minutes=0) is True


def test_handle_is_finished_vs_is_seeding() -> None:
    """is_finished reads status().is_finished (all WANTED data) — distinct from is_seeding (whole
    torrent). A finished-but-not-full-seed pack: is_finished True, is_seeding False."""
    pack = _FakeH(seeding=False, finished=True)
    h = Handle(pack)
    assert h.is_finished() is True
    assert h.is_seeding() is False
    full = _FakeH(seeding=True)  # a full seed is both
    assert Handle(full).is_finished() is True and Handle(full).is_seeding() is True


def test_handle_seeding_pause_resume() -> None:
    fake = _FakeH(seeding=True)
    h = Handle(fake)
    assert h.is_seeding() is True
    assert h.is_paused() is False
    h.pause()
    assert fake.paused is True and h.is_paused() is True
    h.resume()
    assert fake.paused is False and h.is_paused() is False


def test_pause_clears_auto_managed_so_it_sticks(monkeypatch) -> None:
    """The bug: pause() alone doesn't stop an auto_managed torrent (the session auto-manager
    resumes it), so it keeps uploading. Fix: pause() must clear auto_managed first."""
    from stremiosrv.torrent import engine as eng
    monkeypatch.setattr(eng, "_AUTO_MANAGED", 0x20)  # simulate real libtorrent flag present
    fake = _FakeH(seeding=True)
    h = eng.Handle(fake)
    h.pause()
    assert 0x20 in fake.cleared_flags, "pause() must unset_flags(auto_managed) so the pause sticks"
    assert fake.paused is True and h.is_paused() is True


def test_pause_without_lt_flag_still_pauses(monkeypatch) -> None:
    """Binding without torrent_flags (or lt absent): pause() degrades to a plain pause, no crash."""
    from stremiosrv.torrent import engine as eng
    monkeypatch.setattr(eng, "_AUTO_MANAGED", None)
    fake = _FakeH(seeding=True)
    h = eng.Handle(fake)
    h.pause()
    assert fake.cleared_flags == []
    assert fake.paused is True and h.is_paused() is True


def test_handle_set_download_limit() -> None:
    fake = _FakeH()
    h = Handle(fake)
    h.set_download_limit(500_000)
    assert fake.dl_limit == 500_000
