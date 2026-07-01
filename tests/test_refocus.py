"""Regression: Handle.refocus() must not crash when the streaming thread boosts a piece while
refocus is walking the boosted set. Reproduces the field bug where a new episode/seek raised
'RuntimeError: Set changed size during iteration' out of the ASGI request and never started."""
from __future__ import annotations

from stremiosrv.torrent.engine import Handle


class _RacingHandle:
    """lt.torrent_handle stand-in whose have_piece() mutates the owner's live _boosted set,
    emulating a concurrent boost_piece() landing during refocus()."""

    def __init__(self) -> None:
        self.owner: Handle | None = None
        self.prio_calls: list[tuple[int, int]] = []

    def have_piece(self, p: int) -> bool:
        if self.owner is not None:
            self.owner._boosted.add(9000 + p)  # concurrent boost mid-refocus
        return False

    def piece_priority(self, p: int, prio: int) -> None:
        self.prio_calls.append((p, prio))

    def reset_piece_deadline(self, p: int) -> None:
        pass


def test_refocus_survives_concurrent_boost() -> None:
    fake = _RacingHandle()
    h = Handle(fake)
    fake.owner = h
    h._boosted = {1, 2, 3}

    h.refocus()  # must NOT raise "Set changed size during iteration"

    # every snapshotted piece was demoted 7 -> 4 (kept downloading, not dropped)
    assert sorted(fake.prio_calls) == [(1, 4), (2, 4), (3, 4)]
    # the snapshot was swapped out; only the concurrently-added pieces remain tracked
    assert h._boosted == {9001, 9002, 9003}


def test_boost_then_refocus_clears_tracking() -> None:
    class _Quiet:
        def have_piece(self, p: int) -> bool:
            return False

        def piece_priority(self, p: int, prio: int) -> None:
            pass

        def reset_piece_deadline(self, p: int) -> None:
            pass

        def set_piece_deadline(self, p: int, ms: int) -> None:
            pass

    h = Handle(_Quiet())
    h.boost_piece(5, 1000)
    h.boost_piece(6, 1000)
    assert h._boosted == {5, 6}
    h.refocus()
    assert h._boosted == set()
