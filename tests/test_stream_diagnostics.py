"""What a dying stream says about itself, and that the wait budgets are the configured ones.

Both halves exist because of the same real incident: a TV client aborted repeatedly mid-playback
and the only evidence was `piece 749 not available within 30s`. That line names a piece in a torrent
it does not identify, at an offset it does not give, so it cannot be told apart from any other
title stalling on the same box — and the 30s it reports was a hard-coded default nobody could raise
without a rebuild.
"""
import logging

from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.config import Settings
from stremiosrv.stream.fileserver import wait_and_read

IH = "ab" * 20


class FakeHandle:
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

    def boost_piece(self, p, ms):
        pass


# --- the timeout warning ------------------------------------------------------------------------


def test_timeout_warning_names_the_torrent_file_and_offset(tmp_path, caplog):
    """Starved at the requested offset: `0 served` is what distinguishes a cold seek target from a
    window that ran dry partway through."""
    (tmp_path / "f.bin").write_bytes(b"\x00" * 4096)
    h = FakeHandle(plen=1024, have=set())
    with caplog.at_level(logging.WARNING, logger="stremiosrv.stream"):
        assert list(wait_and_read(str(tmp_path), h, 3, 512, 1500,
                                  timeout=0.1, first_timeout=0.1, info_hash=IH)) == []
    msg = caplog.records[-1].getMessage()
    assert "piece 0/100 not available" in msg
    assert IH in msg                    # which torrent — not just which piece
    assert "file 3" in msg              # which file in the pack
    assert "byte 512" in msg            # where it stopped
    assert "range 512-1500" in msg      # what the player had asked for
    assert "0 served" in msg            # nothing was delivered: the stall was AT the seek target


def test_timeout_warning_reports_the_live_cursor_not_the_range_start(tmp_path, caplog):
    """The seek-diagnosis case: bytes flowed, then the window ran dry. The offset must be where the
    read actually reached, otherwise it is just the request echoed back."""
    (tmp_path / "f.bin").write_bytes(b"A" * 1024 + b"\x00" * 1024)
    h = FakeHandle(plen=1024, have={0})  # piece 0 serves; piece 1 never arrives
    with caplog.at_level(logging.WARNING, logger="stremiosrv.stream"):
        chunks = list(wait_and_read(str(tmp_path), h, 0, 0, 2000,
                                    timeout=0.1, first_timeout=0.1, info_hash=IH))
    assert b"".join(chunks) == b"A" * 1024
    msg = caplog.records[-1].getMessage()
    assert "piece 1/100 not available" in msg
    assert "byte 1024" in msg
    assert "1024 served" in msg


def test_disk_error_warning_carries_the_same_fields(tmp_path, caplog):
    """The other way a stream ends early — handle evicted mid-stream, file not on disk yet — needs
    the same identification, and must not itself blow up on an unbound cursor."""
    h = FakeHandle(plen=1024, have={0})  # says piece 0 is ready, but there is no f.bin
    with caplog.at_level(logging.WARNING, logger="stremiosrv.stream"):
        assert list(wait_and_read(str(tmp_path), h, 2, 700, 2000, info_hash=IH)) == []
    msg = caplog.records[-1].getMessage()
    assert "FileNotFoundError" in msg
    assert IH in msg
    assert "file 2" in msg
    assert "byte 700" in msg
    assert "range 700-2000" in msg
    assert "0 served" in msg


def test_missing_info_hash_degrades_to_a_placeholder(tmp_path, caplog):
    """Callers other than the route may not have one; the warning must still format."""
    (tmp_path / "f.bin").write_bytes(b"\x00" * 4096)
    h = FakeHandle(plen=1024, have=set())
    with caplog.at_level(logging.WARNING, logger="stremiosrv.stream"):
        list(wait_and_read(str(tmp_path), h, 0, 0, 100, timeout=0.1, first_timeout=0.1))
    assert "[? file 0" in caplog.records[-1].getMessage()


# --- the budgets are configuration, not constants -------------------------------------------------


class _RouteHandle:
    def has_metadata(self):
        return True

    def is_active(self):
        return False

    def focus_file(self, idx):
        pass

    def refocus(self):
        pass

    def file_size(self, idx):
        return 10

    def file_path(self, idx):
        return "f.bin"

    def note_read_position(self, pos, total):
        pass


class _RouteEngine:
    def get(self, info_hash):
        return _RouteHandle()

    def save_path(self):
        return "/nonexistent"

    def active_torrent_count(self):
        return 1

    def note_stream_open(self, h):
        pass

    def note_stream_close(self, h):
        pass


def _capture(monkeypatch, settings):
    """Drive one real byte-range request and return the kwargs the route handed wait_and_read."""
    from stremiosrv.api import playback

    seen = {}

    def fake(save_path, handle, idx, start, end, **kw):
        seen.update(kw)
        yield b"x" * (end - start + 1)

    monkeypatch.setattr(playback, "wait_and_read", fake)
    client = TestClient(create_app(settings=settings, engine=_RouteEngine()))
    assert client.get(f"/{IH}/0").status_code == 206
    return seen


def test_route_passes_the_configured_timeouts(monkeypatch):
    """The defaults in wait_and_read's signature were unreachable before this: the route passed only
    window_bytes, so no setting could change the wait."""
    seen = _capture(monkeypatch, Settings(stream_piece_timeout=7.5,
                                          stream_first_piece_timeout=99.0))
    assert seen["timeout"] == 7.5
    assert seen["first_timeout"] == 99.0
    assert seen["info_hash"] == IH


def test_timeouts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("STREMIOSRV_STREAM_PIECE_TIMEOUT", "45")
    monkeypatch.setenv("STREMIOSRV_STREAM_FIRST_PIECE_TIMEOUT", "300")
    seen = _capture(monkeypatch, Settings())
    assert seen["timeout"] == 45.0
    assert seen["first_timeout"] == 300.0


def test_defaults_are_unchanged_when_nothing_is_configured(monkeypatch):
    """Existing deployments must behave exactly as they did before the knobs existed."""
    seen = _capture(monkeypatch, Settings())
    assert seen["timeout"] == 30.0
    assert seen["first_timeout"] == 120.0
