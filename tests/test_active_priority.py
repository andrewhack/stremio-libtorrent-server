"""Active-vs-idle download priority: the file being streamed downloads at ACTIVE_FILE_PRIO; when no
stream is open it drops to IDLE_FILE_PRIO (still downloading to completion, but yielding bandwidth to
whatever is being watched now). Supports multiple concurrent streams on one torrent (refcount)."""
from __future__ import annotations

from stremiosrv.torrent.engine import ACTIVE_FILE_PRIO, IDLE_FILE_PRIO, Handle


class _Files:
    def __init__(self, n: int) -> None:
        self._n = n

    def num_files(self) -> int:
        return self._n


class _TI:
    def __init__(self, n: int) -> None:
        self._f = _Files(n)

    def files(self) -> _Files:
        return self._f


class _FakeH:
    def __init__(self, nfiles: int = 3) -> None:
        self._ti = _TI(nfiles)
        self.prio: dict[int, int] = {}      # idx -> current file priority
        self.sequential: bool | None = None

    def torrent_file(self) -> _TI:
        return self._ti

    def prioritize_files(self, prios: list[int]) -> None:
        self.prio = dict(enumerate(prios))

    def set_sequential_download(self, v: bool) -> None:
        self.sequential = v

    def file_priority(self, idx: int, prio: int) -> None:
        self.prio[idx] = prio


def test_focus_is_idle_low_until_a_stream_opens() -> None:
    fake = _FakeH(nfiles=3)
    h = Handle(fake)
    h.focus_file(1)
    # played file downloads (idle-low), the other pack files are skipped entirely
    assert fake.prio[1] == IDLE_FILE_PRIO
    assert fake.prio[0] == 0 and fake.prio[2] == 0
    assert fake.sequential is True
    assert not h.is_active()


def test_active_promotes_and_last_close_demotes() -> None:
    fake = _FakeH(nfiles=3)
    h = Handle(fake)
    h.focus_file(1)

    h.mark_active()  # stream opens
    assert fake.prio[1] == ACTIVE_FILE_PRIO
    assert h.is_active()

    h.mark_active()  # a second concurrent stream on the same torrent
    h.mark_idle()    # one closes -> still watched
    assert fake.prio[1] == ACTIVE_FILE_PRIO
    assert h.is_active()

    h.mark_idle()    # last stream closes -> back to idle-low, still downloading
    assert fake.prio[1] == IDLE_FILE_PRIO
    assert not h.is_active()


def test_focus_while_already_active_uses_active_priority() -> None:
    fake = _FakeH(nfiles=2)
    h = Handle(fake)
    h.mark_active()      # a stream is already open before this file is focused
    h.focus_file(0)
    assert fake.prio[0] == ACTIVE_FILE_PRIO


def test_mark_idle_underflow_is_safe() -> None:
    fake = _FakeH(nfiles=2)
    h = Handle(fake)
    h.focus_file(0)
    h.mark_idle()  # no active stream -> must not go negative or crash
    assert not h.is_active()
    assert fake.prio[0] == IDLE_FILE_PRIO
