"""Server metrics for the appliance suggestion advisor: cache usage + playback stalls."""
import time

from stremiosrv import metrics
from stremiosrv.cache import usage
from stremiosrv.stream.fileserver import wait_and_read
from stremiosrv.transcode import converter


def test_record_stall_and_timeout_snapshot():
    metrics.reset()
    metrics.record_stall(1.5)
    metrics.record_stall(0.5)
    metrics.record_timeout()
    assert metrics.playback_stats() == {
        "stalls": 2, "stallSeconds": 2.0, "timeouts": 1,
        "prefetches": 0, "prefetchBytes": 0, "subtitleSignatureAsks": 0,
        "hlsSessions": 0, "hlsReencodes": 0,
    }


def test_hls_sessions_split_remux_from_reencode():
    """Two numbers, not one: they cost wildly different amounts. An all-`copy` decision is a REMUX
    — the container is rewrapped as HLS and not one frame is re-encoded."""
    metrics.reset()
    metrics.record_hls_session({"video": {"action": "copy"}, "audio": {"action": "copy"}})
    metrics.record_hls_session({"video": {"action": "transcode", "scale_width": 1920},
                                "audio": {"action": "copy"}})
    metrics.record_hls_session({"video": {"action": "copy"}, "audio": {"action": "transcode"}})
    s = metrics.playback_stats()
    assert s["hlsSessions"] == 3
    assert s["hlsReencodes"] == 2  # remuxes = sessions - reencodes = 1


def test_hls_session_without_an_audio_stream_still_counts():
    """decide() omits the audio key entirely for a video-only file; that must not crash or be
    mistaken for a re-encode."""
    metrics.reset()
    metrics.record_hls_session({"video": {"action": "copy"}})
    s = metrics.playback_stats()
    assert s["hlsSessions"] == 1
    assert s["hlsReencodes"] == 0


def test_hls_counters_reset():
    metrics.reset()
    metrics.record_hls_session({"video": {"action": "transcode"}})
    assert metrics.playback_stats()["hlsReencodes"] == 1
    metrics.reset()
    assert metrics.playback_stats() == {
        "stalls": 0, "stallSeconds": 0.0, "timeouts": 0,
        "prefetches": 0, "prefetchBytes": 0, "subtitleSignatureAsks": 0,
        "hlsSessions": 0, "hlsReencodes": 0,
    }


def test_ensure_job_counts_one_session_per_new_job(tmp_path, monkeypatch):
    """The count must follow JOBS, not requests. A player re-fetches master.m3u8 several times per
    playback (three times in one observed capture), and ensure_job returns early for a job that is
    still running — so the counter has to sit behind that check, not in the route."""
    metrics.reset()

    class _Running:
        def poll(self): return None  # never exited, so the job stays "live"

    monkeypatch.setattr(converter.subprocess, "Popen", lambda *a, **k: _Running())
    conv = converter.Converter(str(tmp_path), profile=None)
    decision = {"video": {"action": "copy"}, "audio": {"action": "copy"}}
    conv.ensure_job("job-a", "http://example.invalid/a", decision)
    conv.ensure_job("job-a", "http://example.invalid/a", decision)  # same live job
    conv.ensure_job("job-b", "http://example.invalid/b", decision)
    assert metrics.playback_stats()["hlsSessions"] == 2


def test_subtitle_signature_asks_are_counted_and_reset():
    metrics.reset()
    assert metrics.playback_stats()["subtitleSignatureAsks"] == 0
    metrics.record_subtitle_signature()
    metrics.record_subtitle_signature()
    assert metrics.playback_stats()["subtitleSignatureAsks"] == 2
    metrics.reset()
    assert metrics.playback_stats()["subtitleSignatureAsks"] == 0


def test_record_prefetch_counts_arms_and_requested_bytes():
    metrics.reset()
    metrics.record_prefetch(20 * 1024 * 1024)
    metrics.record_prefetch(4 * 1024 * 1024)
    s = metrics.playback_stats()
    assert s["prefetches"] == 2
    assert s["prefetchBytes"] == 24 * 1024 * 1024


def test_cache_usage(tmp_path):
    (tmp_path / "movie.mkv").write_bytes(b"x" * 5000)
    (tmp_path / "certificates.pem").write_bytes(b"y" * 999)  # protected -> excluded from cacheUsed
    u = usage(str(tmp_path), budget=10000)
    assert u["cacheUsed"] == 5000
    assert u["cacheSize"] == 10000
    assert u["diskTotal"] > 0 and u["diskFree"] >= 0


def test_cache_usage_missing_dir_is_safe():
    assert usage("/no/such/dir", budget=42) == {
        "cacheUsed": 0, "cacheSize": 42, "transcodeUsed": 0, "diskFree": 0, "diskTotal": 0,
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
