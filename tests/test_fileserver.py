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
    """Cold-start: no piece ever arrives -> generator ends cleanly (no ASGI ExceptionGroup) and
    records a timeout, so the player retries instead of seeing a stack trace."""
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
    end the stream cleanly — NOT raise into the ASGI layer, which surfaces as an ExceptionGroup +
    nginx 'upstream prematurely closed connection'."""
    from stremiosrv import metrics
    timeouts = []
    monkeypatch.setattr(metrics, "record_timeout", lambda: timeouts.append(1))
    h = FakeHandle(plen=1024, have={0})  # piece 0 reported available...
    # ...but no f.bin on disk -> open() raises FileNotFoundError inside the generator.
    chunks = list(wait_and_read(str(tmp_path), h, 0, 0, 2000, timeout=0.5, first_timeout=0.5, chunk=1024))
    assert chunks == []        # ended cleanly (no raise)...
    assert timeouts == [1]     # ...recorded + returned
