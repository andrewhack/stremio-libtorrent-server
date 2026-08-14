"""Next-episode prefetch at the Handle / Engine level (fake libtorrent handle, no session)."""
import itertools
import threading

from stremiosrv import metrics
from stremiosrv.config import Settings
from stremiosrv.torrent import prefetch
from stremiosrv.torrent.engine import ACTIVE_FILE_PRIO, IDLE_FILE_PRIO, Engine, Handle

MiB = 1024 * 1024
PLEN = 4 * MiB
EP = 100 * PLEN        # 400 MiB per episode -> 100 pieces each
# Deliberately NOT a multiple of PLEN: a real, unpadded multi-file torrent isn't piece-aligned, so the
# last piece of one file is shared with the first piece of the next. A separate constant (not a change
# to EP) so the other 28 fixtures keep their piece-aligned arithmetic unchanged.
EP_UNALIGNED = 100 * PLEN + 1_000_000
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

    def __init__(self, have=(), size=EP):
        self._ti = _FakeTI(size=size)
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


def test_focus_file_resets_the_read_cursor_on_a_genuine_change():
    # The read cursor tracks bytes read of the FOCUSED file. Carrying stale numbers from the file
    # just left across a switch would let position_reached() fire instantly on the new file (it
    # already satisfied the old file's trigger, and total is compared, not reset).
    h = Handle(_FakeLT())
    h.focus_file(0)
    h.note_read_position(int(EP * 0.95), EP)
    assert h.read_progress() == (int(EP * 0.95), EP)

    h.focus_file(1)  # genuine change (0 -> 1)

    assert h.read_progress() == (0, 0), "the old file's cursor must not leak into the new file"


def test_focus_file_does_not_reset_the_cursor_when_focus_is_unchanged():
    # playback.py calls focus_file(idx) on EVERY range request for the file already being played,
    # not just the first. If the reset ran on that no-op path too, the cursor would never accumulate
    # past 0 during ordinary playback and position_reached() would never fire.
    h = Handle(_FakeLT())
    h.focus_file(0)
    h.note_read_position(int(EP * 0.5), EP)

    h.focus_file(0)  # same index -> early return, not a genuine change

    assert h.read_progress() == (int(EP * 0.5), EP)


def test_file_complete_true_when_every_piece_present():
    assert Handle(_FakeLT(have=range(100))).file_complete(0) is True


def test_file_complete_false_on_a_single_hole():
    h = Handle(_FakeLT(have=set(range(100)) - {57}))
    assert h.file_complete(0) is False


def test_file_complete_false_for_an_untouched_file():
    assert Handle(_FakeLT(have=range(100))).file_complete(1) is False


def test_file_complete_false_for_a_zero_size_file():
    # A file with no bytes can never be "complete"; the size<=0 guard must short-circuit before
    # scanning any pieces (an empty file has no covering piece to check anyway).
    assert Handle(_FakeLT(size=0)).file_complete(0) is False


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


class _StubEngine:
    """_prefetch_tick / _prefetch_loop never touch the libtorrent session, so they can be exercised
    without one (constructing a real Engine would need lt, which the unit suite runs without)."""

    _prefetch_trigger = 0.90
    _prefetch_fraction = 0.05
    _prefetch_max_bytes = 128 * MiB
    _prefetch_tick = Engine._prefetch_tick
    _prefetch_loop = Engine._prefetch_loop


def _armed(*, complete_current=True, paused=False):
    """A 3-episode pack: episode 1 focused and (by default) fully on disk, cursor past the trigger."""
    lt_h = _FakeLT(have=range(100) if complete_current else range(99))
    h = Handle(lt_h)
    h.focus_file(0)
    h.mark_active()
    h.note_read_position(int(EP * 0.95), EP)
    h._paused = paused
    return h, lt_h


def test_tick_arms_head_and_tail_of_the_next_episode():
    metrics.reset()
    h, lt_h = _armed()
    _StubEngine()._prefetch_tick(h)
    # Episode 2 spans pieces 100..199. 5% of 400 MiB = 20 MiB = pieces 100..104; tail = piece 199.
    assert all(lt_h.prio.get(p) == IDLE_FILE_PRIO for p in range(100, 105))
    assert lt_h.prio.get(199) == IDLE_FILE_PRIO
    assert lt_h.prio.get(150) == 0, "only the head and tail may be wanted"
    assert lt_h.deadlines == []
    assert h.focused_index() == 0
    assert metrics.playback_stats()["prefetches"] == 1


def test_tick_arms_only_once_per_next_episode():
    h, lt_h = _armed()
    eng = _StubEngine()
    eng._prefetch_tick(h)
    lt_h.prio.clear()
    eng._prefetch_tick(h)
    assert lt_h.prio == {}


def test_tick_does_not_scan_pieces_before_the_position_gate():
    # The completeness scan is O(pieces-in-file); it must not run on every tick for every torrent.
    h, lt_h = _armed()
    h.note_read_position(int(EP * 0.5), EP)
    lt_h.have_calls = 0
    _StubEngine()._prefetch_tick(h)
    assert lt_h.have_calls == 0, "completeness scan ran before the cheap position gate"
    assert lt_h.prio.get(100) == 0


def test_tick_holds_off_while_the_current_episode_is_incomplete():
    h, lt_h = _armed(complete_current=False)
    _StubEngine()._prefetch_tick(h)
    assert lt_h.prio.get(100) == 0
    assert lt_h.have_calls > 0, "it should have scanned and found the hole"


def test_tick_skips_an_idle_torrent():
    h, lt_h = _armed()
    h.mark_idle()
    _StubEngine()._prefetch_tick(h)
    assert lt_h.prio.get(100) == 0


def test_tick_resumes_a_seed_paused_torrent():
    # SEED_ON_COMPLETE=false leaves a complete torrent paused while it plays from disk. A paused
    # torrent downloads nothing, so without this the feature would silently no-op on those boxes.
    h, lt_h = _armed(paused=True)
    _StubEngine()._prefetch_tick(h)
    assert lt_h.resumed == 1
    assert h.is_paused() is False


def test_tick_leaves_a_running_torrent_alone():
    h, lt_h = _armed()
    _StubEngine()._prefetch_tick(h)
    assert lt_h.resumed == 0


def test_tick_does_nothing_on_the_last_episode():
    h, lt_h = _armed()
    h.focus_file(NFILES - 1)
    h.note_read_position(int(EP * 0.95), EP)
    before = dict(lt_h.prio)
    _StubEngine()._prefetch_tick(h)
    assert lt_h.prio == before


def test_config_prefetch_defaults_off():
    s = Settings()
    assert s.prefetch_next is False
    assert s.prefetch_next_fraction == 0.05
    assert s.prefetch_next_max_bytes == 134_217_728
    assert s.prefetch_trigger_fraction == 0.90


def test_tick_discards_a_stale_decision_when_focus_changes_mid_scan():
    """The streaming thread can call focus_file(nxt) while the tick is still mid-scan (the O(files)
    sweep plus the O(pieces-in-file) completeness scan are hundreds to thousands of round-trips). If
    the tick doesn't notice, prefetch_arm would downgrade the now-focused file's pieces from
    ACTIVE_FILE_PRIO back down to IDLE_FILE_PRIO right after the user pressed Next."""
    h, lt_h = _armed()
    real_have_piece = lt_h.have_piece
    flipped = {"done": False}

    def have_piece_and_flip(p):
        if not flipped["done"]:
            flipped["done"] = True
            h.focus_file(1)  # simulates the streaming thread handling a Next press mid-scan
        return real_have_piece(p)

    lt_h.have_piece = have_piece_and_flip

    armed_calls = []
    real_arm = h.prefetch_arm

    def spy_arm(pieces, *a, **kw):
        armed_calls.append(list(pieces))
        return real_arm(pieces, *a, **kw)

    h.prefetch_arm = spy_arm

    _StubEngine()._prefetch_tick(h)

    assert armed_calls == [], "a stale tick must never call prefetch_arm"
    assert h.focused_index() == 1
    # focus_file(1) itself promoted file 1's pieces to ACTIVE_FILE_PRIO; the stale tick must not
    # clobber that back down to IDLE_FILE_PRIO.
    assert lt_h.prio.get(100) == ACTIVE_FILE_PRIO
    assert lt_h.prio.get(199) == ACTIVE_FILE_PRIO


def test_tick_deduplicates_overlapping_head_and_tail_pieces(monkeypatch):
    """A small next file can make the head range and the trailing-TAIL_BYTES range share pieces.
    prefetch_arm is idempotent so this isn't a correctness bug, but the piece count/byte count that
    feeds `planned` (and the info log) must not be inflated by counting a piece twice."""
    monkeypatch.setattr(prefetch, "head_pieces", lambda *a, **kw: [100, 101, 102])
    monkeypatch.setattr(prefetch, "tail_pieces", lambda *a, **kw: [102, 103])
    h, _lt_h = _armed()
    metrics.reset()

    armed_calls = []
    real_arm = h.prefetch_arm

    def spy_arm(pieces, *a, **kw):
        armed_calls.append(list(pieces))
        return real_arm(pieces, *a, **kw)

    h.prefetch_arm = spy_arm

    _StubEngine()._prefetch_tick(h)

    assert armed_calls == [[100, 101, 102, 103]], "duplicate piece 102 must be collapsed, order kept"
    assert metrics.playback_stats()["prefetchBytes"] == 4 * PLEN


def test_tick_returns_when_metadata_is_not_yet_available():
    class _NoMetaStatus:
        has_metadata = False
        info_hashes = _IH()

    h, lt_h = _armed()
    lt_h.status = _NoMetaStatus  # calling it (lt_h.status()) constructs a has_metadata=False instance
    before = dict(lt_h.prio)
    _StubEngine()._prefetch_tick(h)
    assert lt_h.prio == before


def test_tick_returns_when_nothing_is_focused_yet():
    lt_h = _FakeLT(have=range(100))
    h = Handle(lt_h)
    h.mark_active()
    _StubEngine()._prefetch_tick(h)
    assert lt_h.prio == {}


def test_tick_returns_when_no_pieces_are_planned(monkeypatch):
    """Today's real geometry can't produce this (MIN_VIDEO_BYTES / TAIL_BYTES in prefetch.py guarantee
    at least the tail piece), but the guard exists to stop an empty plan from being armed and
    permanently marked prefetched -- force the inputs directly to prove it holds."""
    monkeypatch.setattr(prefetch, "head_pieces", lambda *a, **kw: [])
    monkeypatch.setattr(prefetch, "tail_pieces", lambda *a, **kw: [])
    h, lt_h = _armed()
    before = dict(lt_h.prio)
    _StubEngine()._prefetch_tick(h)
    assert lt_h.prio == before
    assert h.is_prefetched(1) is False


def test_prefetch_loop_survives_a_raising_tick():
    """The resilience guarantee ("never let the prefetch thread die") lives in _prefetch_loop's
    except clause, not in _prefetch_tick -- it has to be exercised at the loop level to prove it."""

    class _FastEvent(threading.Event):
        """wait() returns immediately regardless of timeout, so the loop under test can be driven
        without sleeping for the real PREFETCH_INTERVAL (5s)."""

        def wait(self, timeout=None):
            return super().wait(0)

    class _RaisingHandle:
        def __init__(self, stop_event):
            self._stop_event = stop_event
            self.calls = 0

        def is_active(self):
            self.calls += 1
            self._stop_event.set()  # stop after this one tick so the loop under test returns
            raise RuntimeError("boom")

    eng = _StubEngine()
    eng._stop = _FastEvent()
    eng._torrents = {"deadbeef": _RaisingHandle(eng._stop)}

    eng._prefetch_loop()  # must return normally, not raise

    assert eng._torrents["deadbeef"].calls == 1


def test_route_records_the_read_position(tmp_path):
    """The streaming generator must feed the prefetch loop, or the trigger never fires."""
    from fastapi.testclient import TestClient

    from stremiosrv.app import create_app

    (tmp_path / "ep.mkv").write_bytes(b"x" * 4096)
    recorded: list[tuple[int, int]] = []

    class _H:
        def has_metadata(self): return True
        def is_active(self): return True
        def focus_file(self, idx): pass
        def refocus(self): pass
        def file_size(self, idx): return 4096
        def file_path(self, idx): return "ep.mkv"
        def piece_length(self): return 4096
        def file_offset(self, idx): return 0
        def num_pieces(self): return 1
        def have_piece(self, i): return True
        def boost_piece(self, p, ms): pass
        def note_read_position(self, pos, total): recorded.append((pos, total))

    class _E:
        def __init__(self): self._h = _H()
        def get(self, ih): return self._h
        def save_path(self): return str(tmp_path)
        def active_torrent_count(self): return 1
        def note_stream_open(self, h): pass
        def note_stream_close(self, h): pass

    c = TestClient(create_app(engine=_E()))
    r = c.get(f"/{'ab' * 20}/0", headers={"Range": "bytes=0-4095"})
    assert r.status_code == 206
    assert recorded[-1] == (4096, 4096)


def test_route_records_an_absolute_position_for_a_non_zero_start(tmp_path):
    """pos is seeded from `start`, not 0, so a seek mid-file must still record an absolute file
    position -- position_reached() compares it against the whole-file total, not the range size."""
    from fastapi.testclient import TestClient

    from stremiosrv.app import create_app

    file_size = 10_000
    range_start = 100
    (tmp_path / "ep.mkv").write_bytes(b"x" * file_size)
    recorded: list[tuple[int, int]] = []

    class _H:
        def has_metadata(self): return True
        def is_active(self): return True
        def focus_file(self, idx): pass
        def refocus(self): pass
        def file_size(self, idx): return file_size
        def file_path(self, idx): return "ep.mkv"
        def piece_length(self): return file_size
        def file_offset(self, idx): return 0
        def num_pieces(self): return 1
        def have_piece(self, i): return True
        def boost_piece(self, p, ms): pass
        def note_read_position(self, pos, total): recorded.append((pos, total))

    class _E:
        def __init__(self): self._h = _H()
        def get(self, ih): return self._h
        def save_path(self): return str(tmp_path)
        def active_torrent_count(self): return 1
        def note_stream_open(self, h): pass
        def note_stream_close(self, h): pass

    c = TestClient(create_app(engine=_E()))
    r = c.get(f"/{'ab' * 20}/0", headers={"Range": f"bytes={range_start}-{file_size - 1}"})
    assert r.status_code == 206
    assert len(recorded) == 1, "range fits in a single 262144-byte chunk"
    # Absolute position: end + 1 == file_size. A `pos` seeded from 0 instead of `start` would have
    # recorded (file_size - range_start, file_size) instead.
    assert recorded[-1] == (file_size, file_size)


def test_route_records_positions_monotonically_across_multiple_chunks(tmp_path):
    """A file spanning several 262144-byte wait_and_read chunks must get one note_read_position
    call per chunk, with the recorded position increasing monotonically up to the final total."""
    from fastapi.testclient import TestClient

    from stremiosrv.app import create_app

    file_size = 600_000  # > 2 * the 262144-byte read chunk -> at least 3 chunks
    (tmp_path / "ep.mkv").write_bytes(b"x" * file_size)
    recorded: list[tuple[int, int]] = []

    class _H:
        def has_metadata(self): return True
        def is_active(self): return True
        def focus_file(self, idx): pass
        def refocus(self): pass
        def file_size(self, idx): return file_size
        def file_path(self, idx): return "ep.mkv"
        def piece_length(self): return file_size
        def file_offset(self, idx): return 0
        def num_pieces(self): return 1
        def have_piece(self, i): return True
        def boost_piece(self, p, ms): pass
        def note_read_position(self, pos, total): recorded.append((pos, total))

    class _E:
        def __init__(self): self._h = _H()
        def get(self, ih): return self._h
        def save_path(self): return str(tmp_path)
        def active_torrent_count(self): return 1
        def note_stream_open(self, h): pass
        def note_stream_close(self, h): pass

    c = TestClient(create_app(engine=_E()))
    r = c.get(f"/{'ab' * 20}/0", headers={"Range": f"bytes=0-{file_size - 1}"})
    assert r.status_code == 206
    assert len(recorded) > 1, "file spans multiple chunks; the cursor must be recorded per chunk"
    positions = [pos for pos, _ in recorded]
    assert all(a < b for a, b in itertools.pairwise(positions)), \
        "recorded positions must increase monotonically"
    assert all(total == file_size for _, total in recorded), "total must stay the whole-file size"
    assert recorded[-1] == (file_size, file_size)


def test_switching_to_the_prefetched_episode_downloads_the_whole_file():
    """The requirement: pressing Next must resume the full download of the prefetched episode.

    focus_file returns early when _focused_idx already equals the requested index, so if prefetch
    ever claimed focus, this switch would be a no-op and every piece past the prefetched head would
    stay at priority 0 forever. libtorrent's file-level priority write overwrites the piece-level
    one, which is what makes the promotion happen — the fake models that."""
    h, lt_h = _armed()
    _StubEngine()._prefetch_tick(h)
    assert lt_h.prio.get(100) == IDLE_FILE_PRIO, "head should be armed"
    assert lt_h.prio.get(150) == 0, "middle of episode 2 should not be wanted yet"
    assert h.focused_index() == 0, "prefetch must not steal focus"

    h.focus_file(1)  # the user presses Next

    assert h.focused_index() == 1
    assert all(lt_h.prio.get(p) == ACTIVE_FILE_PRIO for p in range(100, 200)), \
        "episode 2 stranded at the prefetched head — focus_file was a no-op"


def test_tick_does_not_arm_immediately_after_a_focus_change_leaves_the_cursor_stale():
    """Before the Fix 1 cursor reset, a genuine focus change left _read_pos/_read_total holding the
    OLD file's numbers. Those numbers already satisfied the 90% trigger (that's what got the new
    file prefetched in the first place), so -- on a torrent where the newly-focused file also
    happens to already be complete, e.g. a fully-cached pack -- the very next tick, before a single
    byte of the new file has been read, would see position_reached() return True from stale data and
    arm the file AFTER it too. The safety invariant (file_complete) still stops it from competing
    with playback, but the trigger itself must not fire on data left over from a different file."""
    h, lt_h = _armed()  # episode 0 focused + complete, cursor at 95%
    _StubEngine()._prefetch_tick(h)  # arms episode 1
    assert h.is_prefetched(1) is True

    h.focus_file(1)  # the user presses Next
    assert h.read_progress() == (0, 0), "sanity: the Fix 1 reset must have already run"

    lt_h.prio.clear()
    # Pretend episode 1 (now focused) is ALSO already fully on disk -- e.g. a fully-cached pack --
    # so the completeness gate alone would not block arming episode 2. That isolates the position
    # gate as the only thing standing between this tick and a premature arm.
    lt_h._have.update(range(100, 200))

    _StubEngine()._prefetch_tick(h)

    assert lt_h.prio == {}, "a stale cursor must never arm the next-next episode at t=0 of a switch"


def test_prefetched_head_can_be_rearmed_after_focus_moves_away_and_back():
    """focus_file's prioritize_files overwrites EVERY piece priority on the torrent on a genuine
    change, wiping any previously armed head back to 0. Without clearing _prefetched too, that file
    index could never be re-armed even though its priorities really were reset -- the benefit would
    be silently lost forever."""
    h, lt_h = _armed()  # episode 0 (idx 0) focused + complete, cursor past the trigger
    _StubEngine()._prefetch_tick(h)  # arms episode 1's head
    assert lt_h.prio.get(100) == IDLE_FILE_PRIO
    assert h.is_prefetched(1) is True

    h.focus_file(2)  # focus moves away entirely (e.g. the user jumps ahead)
    assert lt_h.prio.get(100) == 0, "sanity: focus_file really did wipe the armed head"

    h.focus_file(0)  # ...and back to episode 1's predecessor
    h.note_read_position(int(EP * 0.95), EP)  # a fresh past-the-trigger cursor for episode 0
    lt_h.prio.clear()  # observation-only reset so the next assertion is unambiguous

    _StubEngine()._prefetch_tick(h)

    assert lt_h.prio.get(100) == IDLE_FILE_PRIO, "the wiped head must be re-armable once focus returns"
    assert h.is_prefetched(1) is True


def test_unaligned_boundary_piece_is_already_downloaded_when_armed():
    """Real, unpadded multi-file torrents aren't piece-aligned: the last piece of the file being
    played can be the SAME physical piece as the first piece of the next file's head range.
    file_complete(idx) is what makes prefetch_arm's unconditional piece_priority() write safe in that
    case -- it guarantees every piece of the file being played (idx) is already on disk before arming
    runs, so downgrading a shared piece's priority can never pull it out from under playback. Every
    other fixture in this file uses piece-aligned geometry (EP = 100 * PLEN), so this is the only
    test that would catch a future change that relaxes file_complete's all-or-nothing check."""
    # File 0 spans pieces 0..100 with this unaligned size -- piece 100 is the boundary, shared with
    # the start of file 1's byte range.
    lt_h = _FakeLT(have=range(101), size=EP_UNALIGNED)
    h = Handle(lt_h)
    h.focus_file(0)
    h.mark_active()
    h.note_read_position(int(EP_UNALIGNED * 0.95), EP_UNALIGNED)

    boundary_piece = EP_UNALIGNED // PLEN  # last piece of file 0 == first piece of file 1's head
    assert boundary_piece == 100
    assert h.file_complete(0) is True
    assert lt_h.have_piece(boundary_piece) is True, "file_complete(0) must guarantee this before arming"
    assert lt_h.prio.get(boundary_piece) == ACTIVE_FILE_PRIO, "sanity: playing file 0 owns it for now"

    _StubEngine()._prefetch_tick(h)

    # The tick armed file 1's head, which downgrades the shared boundary piece from ACTIVE to IDLE --
    # harmless only because file_complete(0) already guaranteed it was on disk before this ran.
    assert lt_h.prio.get(boundary_piece) == IDLE_FILE_PRIO
    assert boundary_piece in lt_h._have, "the piece must still be on disk -- arming never fetches"
