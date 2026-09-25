"""Unit test for the file server's piece-boundary safety (no libtorrent needed)."""
import pytest

from stremiosrv.stream.fileserver import VIEWER_FIRST_BYTES, wait_and_read
from stremiosrv.torrent.engine import Handle


class FakeHandle:
    """Minimal handle: piece 0 present, piece 1 'not downloaded' (sparse zeros on disk)."""

    def __init__(self, plen: int, have: set[int]):
        self._plen = plen
        self._have = have

    def piece_length(self):
        return self._plen

    def file_offset(self, idx):
        return 0

    def file_path(self, idx):
        return "f.bin"

    def num_pieces(self):
        return 100

    def have_piece(self, i):
        return i in self._have

    def boost_piece(self, p, ms):  # recorded calls not needed for this test
        pass


def test_read_never_crosses_into_unavailable_piece(tmp_path):
    plen = 1024
    (tmp_path / "f.bin").write_bytes(b"A" * plen + b"\x00" * plen)  # piece0=real, piece1=sparse
    h = FakeHandle(plen, have={0})
    # Range starts mid-piece-0 and extends into piece-1; piece-1 is not available -> the stream
    # ends cleanly at the piece boundary (no raise), yielding only the valid tail of piece 0.
    chunks = list(wait_and_read(str(tmp_path), h, 0, 512, 1500, timeout=0.5, first_timeout=0.5, chunk=1024))
    data = b"".join(chunks)
    # Must yield ONLY the valid tail of piece 0 — never the sparse zeros of piece 1.
    assert data == b"A" * 512
    assert b"\x00" not in data


def test_timeout_ends_stream_gracefully_without_raising(tmp_path, monkeypatch):
    """Cold-start: no piece ever arrives -> generator ends cleanly and records a timeout, so the
    player retries. Scope note: this asserts the *generator* does not raise. It says nothing about
    the ASGI layer — the short body still trips uvicorn's Content-Length check, which is covered by
    test_truncated_stream.py."""
    from stremiosrv import metrics
    timeouts = []
    monkeypatch.setattr(metrics, "record_timeout", lambda: timeouts.append(1))
    (tmp_path / "f.bin").write_bytes(b"\x00" * 4096)
    h = FakeHandle(plen=1024, have=set())  # nothing downloaded
    chunks = list(wait_and_read(str(tmp_path), h, 0, 0, 2000, timeout=0.2, first_timeout=0.2, chunk=1024))
    assert chunks == []          # no data, but...
    assert timeouts == [1]       # ...recorded the timeout and returned (did NOT raise)


def test_disk_error_ends_stream_gracefully(tmp_path, monkeypatch):
    """A mid-stream failure (file not on disk yet, handle removed by the evictor, or disk I/O) must
    end the stream cleanly rather than propagating out of the generator. Same scope note as above:
    the ASGI-level consequence is covered by test_truncated_stream.py."""
    from stremiosrv import metrics
    timeouts = []
    monkeypatch.setattr(metrics, "record_timeout", lambda: timeouts.append(1))
    h = FakeHandle(plen=1024, have={0})  # piece 0 reported available...
    # ...but no f.bin on disk -> open() raises FileNotFoundError inside the generator.
    chunks = list(wait_and_read(str(tmp_path), h, 0, 0, 2000, timeout=0.5, first_timeout=0.5, chunk=1024))
    assert chunks == []        # ended cleanly (no raise)...
    assert timeouts == [1]     # ...recorded + returned


# --- A second reader of a file someone is watching (api/embedded_ass.py's private reader). ---


class RecordingHandle(FakeHandle):
    """Every piece present; records each boost as (piece, deadline_ms, keep_existing)."""

    def __init__(self, plen: int, have: set[int]):
        super().__init__(plen, have)
        self.boosts: list[tuple[int, int, bool]] = []

    def boost_piece(self, p, ms, keep_existing=False):
        self.boosts.append((p, ms, keep_existing))


class OneDeadlinePerPiece:
    """A libtorrent torrent_handle stand-in that keeps ONE deadline per piece, the last call
    winning, as libtorrent does."""

    def __init__(self):
        self.deadlines: dict[int, int] = {}

    def piece_priority(self, p, prio):
        pass

    def set_piece_deadline(self, p, ms):
        self.deadlines[p] = ms


class SharedHandle(Handle):
    """The real engine Handle -- its real boost_piece -- over OneDeadlinePerPiece, with a file of
    `pieces` pieces of `plen` bytes, every one of them downloaded."""

    def __init__(self, plen: int, pieces: int):
        super().__init__(OneDeadlinePerPiece())
        self._plen, self._pieces = plen, pieces

    def piece_length(self):
        return self._plen

    def num_pieces(self):
        return self._pieces

    def file_offset(self, idx):
        return 0

    def file_path(self, idx):
        return "f.bin"

    def have_piece(self, i):
        return True

    def deadlines(self) -> dict[int, int]:
        return self._h.deadlines


class ArrivingHandle(FakeHandle):
    """Piece 0 arrives after the reader has waited for it once (a stall); piece 1 never does (a
    timeout). One read of this handle produces both events a reader can count."""

    def __init__(self, plen: int):
        super().__init__(plen, have=set())
        self.asked = 0

    def have_piece(self, i):
        if i == 0:
            self.asked += 1
            return self.asked > 1
        return False


def _read_piece(tmp_path, handle, piece, **kw):
    plen = handle.piece_length()
    return list(wait_and_read(str(tmp_path), handle, 0, piece * plen, piece * plen + plen - 1,
                              window_bytes=8 * plen, chunk=plen, **kw))


def test_a_reader_that_yields_never_moves_the_viewers_deadlines(tmp_path):
    """The embedded-ASS reader reads the file a TV is playing. After a seek its window starts
    behind the TV's new position, so the two boost windows overlap. libtorrent keeps one deadline
    per piece and the last call wins: re-deadlining the overlap would put the TV's playhead pieces
    behind the reader's. It must leave them alone, and put its own after the viewer's next
    32 MiB."""
    plen = 1024
    (tmp_path / "f.bin").write_bytes(b"A" * plen * 64)
    h = SharedHandle(plen, 64)
    _read_piece(tmp_path, h, 30)  # the TV, just after a seek to piece 30
    viewer = dict(h.deadlines())
    assert sorted(viewer) == list(range(30, 39)), "the viewer's window is pieces 30..38"
    _read_piece(tmp_path, h, 24, count=False, yield_to_viewer=True)  # its window starts behind
    after = h.deadlines()
    assert {p: after[p] for p in viewer} == viewer, "the reader moved the viewer's deadlines"
    offset = (VIEWER_FIRST_BYTES // plen) * 50
    own = {p: after[p] for p in range(24, 30)}
    assert own == {p: offset + (p - 24) * 50 for p in range(24, 30)}
    assert min(after[p] for p in range(24, 30)) > max(viewer.values())


def test_the_viewers_boosts_still_replace_a_readers(tmp_path):
    """The other order: the reader first, then the viewer. The viewer always wins."""
    plen = 1024
    (tmp_path / "f.bin").write_bytes(b"A" * plen * 64)
    h = SharedHandle(plen, 64)
    _read_piece(tmp_path, h, 24, count=False, yield_to_viewer=True)
    _read_piece(tmp_path, h, 30)
    assert {p: h.deadlines()[p] for p in range(30, 33)} == {30: 0, 31: 50, 32: 100}


@pytest.mark.parametrize("plen", [256 * 1024, 1 << 20, 4 << 20])
def test_a_yielding_readers_deadlines_start_after_the_viewers_next_32_mib(tmp_path, plen):
    """Bytes, not pieces: 32 MiB of the viewer's read-ahead comes first on every torrent."""
    (tmp_path / "f.bin").write_bytes(b"A" * 16)
    h = RecordingHandle(plen, {0, 1, 2, 3})
    list(wait_and_read(str(tmp_path), h, 0, 0, 15, chunk=16, count=False, yield_to_viewer=True))
    first_piece, first_delay, keep_existing = h.boosts[0]
    assert (first_piece, first_delay) == (0, (32 * 2**20 // plen) * 50)
    assert keep_existing is True


def test_an_uncounted_reader_records_no_stall_and_no_timeout(tmp_path, monkeypatch):
    """A second reader's waits are not playback stalls. Counted, they would show a starved box on
    /stats.json whenever a TV has a styled subtitle track selected."""
    from stremiosrv import metrics
    seen = []
    monkeypatch.setattr(metrics, "record_timeout", lambda: seen.append("timeout"))
    monkeypatch.setattr(metrics, "record_stall", lambda s: seen.append("stall"))
    plen = 1024
    (tmp_path / "f.bin").write_bytes(b"A" * plen * 2)
    kw = dict(timeout=0.2, first_timeout=0.2, chunk=plen)
    counted = list(wait_and_read(str(tmp_path), ArrivingHandle(plen), 0, 0, plen * 2 - 1, **kw))
    assert counted == [b"A" * plen]
    assert seen == ["stall", "timeout"], "the handle must produce both events for this to prove it"
    seen.clear()
    quiet = list(wait_and_read(str(tmp_path), ArrivingHandle(plen), 0, 0, plen * 2 - 1,
                               count=False, **kw))
    assert quiet == [b"A" * plen]
    assert seen == []


def test_an_uncounted_reader_records_no_timeout_on_a_disk_error(tmp_path, monkeypatch):
    from stremiosrv import metrics
    seen = []
    monkeypatch.setattr(metrics, "record_timeout", lambda: seen.append("timeout"))
    h = FakeHandle(plen=1024, have={0})  # piece 0 present, but no f.bin on disk
    kw = dict(timeout=0.5, first_timeout=0.5, chunk=1024)
    assert list(wait_and_read(str(tmp_path), h, 0, 0, 2000, **kw)) == []
    assert seen == ["timeout"], "the disk error must be counted by default for this to prove it"
    seen.clear()
    assert list(wait_and_read(str(tmp_path), h, 0, 0, 2000, count=False, **kw)) == []
    assert seen == []
