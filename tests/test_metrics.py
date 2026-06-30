"""Server metrics for the appliance suggestion advisor: cache usage + playback stalls."""
import time

from stremiosrv import metrics
from stremiosrv.cache import usage
from stremiosrv.stream.fileserver import wait_and_read


def test_record_stall_and_timeout_snapshot():
    metrics.reset()
    metrics.record_stall(1.5)
    metrics.record_stall(0.5)
    metrics.record_timeout()
    assert metrics.playback_stats() == {"stalls": 2, "stallSeconds": 2.0, "timeouts": 1}


def test_cache_usage(tmp_path):
    (tmp_path / "movie.mkv").write_bytes(b"x" * 5000)
    (tmp_path / "certificates.pem").write_bytes(b"y" * 999)  # protected -> excluded from cacheUsed
    u = usage(str(tmp_path), budget=10000)
    assert u["cacheUsed"] == 5000
    assert u["cacheSize"] == 10000
    assert u["diskTotal"] > 0 and u["diskFree"] >= 0


def test_cache_usage_missing_dir_is_safe():
    assert usage("/no/such/dir", budget=42) == {
        "cacheUsed": 0, "cacheSize": 42, "diskFree": 0, "diskTotal": 0,
    }


class _Handle:
    """Fake torrent handle whose single piece becomes available at a wall-clock time."""

    def __init__(self, plen: int, ready_at: float):
        self._plen = plen
        self._ready_at = ready_at

    def piece_length(self): return self._plen
    def file_offset(self, idx): return 0
    def file_path(self, idx): return "f.bin"
    def num_pieces(self): return 100
    def have_piece(self, i): return time.time() >= self._ready_at
    def boost_piece(self, p, ms): pass


def test_wait_and_read_records_stall(tmp_path):
    metrics.reset()
    plen = 1024
    (tmp_path / "f.bin").write_bytes(b"A" * plen)
    h = _Handle(plen, ready_at=time.time() + 0.25)  # piece arrives shortly -> exactly one stall
    data = b"".join(wait_and_read(str(tmp_path), h, 0, 0, plen - 1, timeout=3.0, chunk=plen))
    assert data == b"A" * plen
    snap = metrics.playback_stats()
    assert snap["stalls"] == 1
    assert snap["stallSeconds"] > 0
    assert snap["timeouts"] == 0


def test_wait_and_read_records_timeout(tmp_path):
    metrics.reset()
    plen = 1024
    (tmp_path / "f.bin").write_bytes(b"A" * plen)
    h = _Handle(plen, ready_at=time.time() + 9999)  # never arrives within the timeout
    # first_timeout too (the first piece uses it) so the test is fast, not 120s. wait_and_read no
    # longer raises — it ends the stream cleanly — so we just drain it.
    chunks = list(wait_and_read(str(tmp_path), h, 0, 0, plen - 1,
                                timeout=0.4, first_timeout=0.4, chunk=plen))
    assert chunks == []  # ended cleanly (graceful), did not raise
    snap = metrics.playback_stats()
    assert snap["timeouts"] == 1
    assert snap["stalls"] == 0  # a timeout is not also counted as a (recovered) stall
