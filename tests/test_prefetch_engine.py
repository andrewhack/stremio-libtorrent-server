"""Next-episode prefetch at the Handle / Engine level (fake libtorrent handle, no session)."""
from stremiosrv.torrent.engine import IDLE_FILE_PRIO, Handle

MiB = 1024 * 1024
PLEN = 4 * MiB
EP = 100 * PLEN        # 400 MiB per episode -> 100 pieces each
NFILES = 3


class _IH:
    v1 = "ab" * 20


class _FakeStatus:
    has_metadata = True
    info_hashes = _IH()


class _FakeFiles:
    def __init__(self, n=NFILES, size=EP):
        self._n, self._size = n, size

    def num_files(self):
        return self._n

    def file_size(self, i):
        return self._size

    def file_offset(self, i):
        return i * self._size

    def file_path(self, i):
        return f"Show.S01E{i + 1:02d}.mkv"

    def file_name(self, i):
        return self.file_path(i)


class _FakeTI:
    def __init__(self, n=NFILES, size=EP, plen=PLEN):
        self._files = _FakeFiles(n, size)
        self._plen = plen

    def files(self):
        return self._files

    def piece_length(self):
        return self._plen

    def num_pieces(self):
        return (self._files.num_files() * self._files.file_size(0)) // self._plen

    def name(self):
        return "Show.S01"


class _FakeLT:
    """lt.torrent_handle stand-in that records every priority write and every deadline."""

    def __init__(self, have=()):
        self._ti = _FakeTI()
        self._have = set(have)
        self.prio: dict[int, int] = {}
        self.deadlines: list[tuple[int, int]] = []
        self.resumed = 0
        self.have_calls = 0

    def status(self):
        return _FakeStatus()

    def torrent_file(self):
        return self._ti

    def have_piece(self, p):
        self.have_calls += 1
        return p in self._have

    def piece_priority(self, p, v):
        self.prio[p] = v

    def set_piece_deadline(self, p, ms):
        self.deadlines.append((p, ms))

    def _write_file(self, i, v):
        fs = self._ti.files()
        off, size = fs.file_offset(i), fs.file_size(i)
        for p in range(off // PLEN, (off + size - 1) // PLEN + 1):
            self.prio[p] = v

    def prioritize_files(self, prios):
        # libtorrent's file-level write overwrites piece-level priorities. That is the mechanism
        # resume-on-switch depends on, so the fake must model it or the key test proves nothing.
        for i, pr in enumerate(prios):
            self._write_file(i, pr)

    def file_priority(self, i, v):
        self._write_file(i, v)

    def set_sequential_download(self, v):
        pass

    def resume(self):
        self.resumed += 1


def test_note_read_position_round_trips():
    h = Handle(_FakeLT())
    assert h.read_progress() == (0, 0)
    h.note_read_position(123, 456)
    assert h.read_progress() == (123, 456)


def test_focused_index_is_none_before_first_focus():
    h = Handle(_FakeLT())
    assert h.focused_index() is None
    h.focus_file(1)
    assert h.focused_index() == 1


def test_file_complete_true_when_every_piece_present():
    assert Handle(_FakeLT(have=range(0, 100))).file_complete(0) is True


def test_file_complete_false_on_a_single_hole():
    h = Handle(_FakeLT(have=set(range(0, 100)) - {57}))
    assert h.file_complete(0) is False


def test_file_complete_false_for_an_untouched_file():
    assert Handle(_FakeLT(have=range(0, 100))).file_complete(1) is False


def test_prefetch_arm_writes_low_priority_and_no_deadlines():
    lt_h = _FakeLT()
    h = Handle(lt_h)
    h.prefetch_arm([100, 101, 199])
    assert lt_h.prio == {100: IDLE_FILE_PRIO, 101: IDLE_FILE_PRIO, 199: IDLE_FILE_PRIO}
    assert lt_h.deadlines == [], "prefetch must never use deadlines — they are the playhead's"


def test_prefetch_arm_does_not_claim_focus():
    # focus_file returns early when _focused_idx already matches, so claiming focus here would make
    # the later real play of that file a no-op and strand it at the prefetched head.
    lt_h = _FakeLT()
    h = Handle(lt_h)
    h.focus_file(0)
    h.prefetch_arm([100, 101])
    assert h.focused_index() == 0


def test_prefetch_arm_survives_a_raising_binding():
    class _Bad(_FakeLT):
        def piece_priority(self, p, v):
            raise RuntimeError("binding blew up")

    Handle(_Bad()).prefetch_arm([1, 2, 3])  # must not raise


def test_prefetched_bookkeeping():
    h = Handle(_FakeLT())
    assert h.is_prefetched(1) is False
    h.mark_prefetched(1)
    assert h.is_prefetched(1) is True
    assert h.is_prefetched(2) is False
