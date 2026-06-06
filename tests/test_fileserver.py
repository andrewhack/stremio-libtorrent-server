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
    # Range starts mid-piece-0 and extends into piece-1; piece-1 is not available.
    chunks = []
    try:
        for c in wait_and_read(str(tmp_path), h, 0, 512, 1500, timeout=0.5, chunk=1024):
            chunks.append(c)
    except TimeoutError:
        pass  # expected once we reach the unavailable piece
    data = b"".join(chunks)
    # Must yield ONLY the valid tail of piece 0 — never the sparse zeros of piece 1.
    assert data == b"A" * 512
    assert b"\x00" not in data
