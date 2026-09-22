"""Unit test for the file server's piece-boundary safety (no libtorrent needed)."""
from stremiosrv.stream.fileserver import wait_and_read


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
    """Every piece present; records each boost as (piece, deadline_ms)."""

    def __init__(self, plen: int, have: set[int]):
        super().__init__(plen, have)
        self.boosts: list[tuple[int, int]] = []

    def boost_piece(self, p, ms):
        self.boosts.append((p, ms))


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


def test_deadline_offset_makes_every_deadline_later(tmp_path):
    """The embedded-ASS reader reads a file the viewer is playing. Each deadline it sets must come
    after the viewer's own, so the pieces under the viewer's playhead are fetched first."""
    plen = 1024
    (tmp_path / "f.bin").write_bytes(b"A" * plen * 4)
    viewer, reader = RecordingHandle(plen, {0, 1, 2, 3}), RecordingHandle(plen, {0, 1, 2, 3})
    kw = dict(window_bytes=plen * 4, chunk=plen)
    list(wait_and_read(str(tmp_path), viewer, 0, 0, plen * 4 - 1, **kw))
    list(wait_and_read(str(tmp_path), reader, 0, 0, plen * 4 - 1, deadline_offset_ms=2000, **kw))
    assert viewer.boosts, "nothing was boosted -- the comparison below would prove nothing"
    assert [p for p, _ in reader.boosts] == [p for p, _ in viewer.boosts]
    assert [ms for _, ms in reader.boosts] == [ms + 2000 for _, ms in viewer.boosts]


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
