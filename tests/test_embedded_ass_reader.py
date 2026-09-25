"""ffmpeg's private reader for the embedded-ASS routes: who may use it, and what it must not do.

The reader serves a torrent file the TV is already playing. Going through the stream route instead
would `refocus()` the torrent on every request and take the TV's playhead priority away.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stremiosrv.api import embedded_ass
from stremiosrv.app import create_app

IH = "ab" * 20
DATA = bytes(range(256)) * 16  # 4 KiB, 4 pieces of 1 KiB


class Handle:
    """A torrent with metadata, one file, every piece present. Calling anything that would move
    the viewer's playhead or mark the torrent watched fails the test."""

    def has_metadata(self):
        return True

    def num_files(self):
        return 1

    def file_size(self, idx):
        return len(DATA)

    def file_path(self, idx):
        return "clip.mkv"

    def piece_length(self):
        return 1024

    def file_offset(self, idx):
        return 0

    def num_pieces(self):
        return 4

    def have_piece(self, i):
        return True

    def boost_piece(self, p, ms, keep_existing=False):
        pass

    def refocus(self):
        raise AssertionError("the private reader must never refocus: it drops the TV's deadlines")

    def focus_file(self, idx):
        raise AssertionError("the private reader must never change the torrent's focus")


class Engine:
    def __init__(self, root, handle=None):
        self.root, self.handle = str(root), handle or Handle()

    def get(self, info_hash):
        return self.handle if info_hash == IH else None

    def save_path(self):
        return self.root

    def note_stream_open(self, h):
        raise AssertionError("the private reader must never mark the torrent watched")

    def add(self, *a, **k):
        raise AssertionError("the private reader must never add a torrent")


@pytest.fixture
def engine(tmp_path):
    (tmp_path / "clip.mkv").write_bytes(DATA)
    return Engine(tmp_path)


def _client(engine, host="127.0.0.1"):
    return TestClient(create_app(engine=engine), client=(host, 45678))


def _path(secret=None, ih=IH, idx=0):
    return f"/_embedded-ass-read/{secret or embedded_ass._SECRET}/{ih}/{idx}"


def test_ffmpeg_reads_byte_ranges(engine):
    r = _client(engine).get(_path(), headers={"Range": "bytes=1000-2999"})
    assert r.status_code == 206
    assert r.content == DATA[1000:3000]
    assert r.headers["content-range"] == f"bytes 1000-2999/{len(DATA)}"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/x-matroska"


def test_a_range_past_the_end_is_416(engine):
    r = _client(engine).get(_path(), headers={"Range": f"bytes={len(DATA)}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(DATA)}"


@pytest.mark.parametrize("why", ["wrong secret", "not loopback", "came through nginx"])
def test_anyone_but_this_process_s_ffmpeg_gets_404(engine, why):
    client = _client(engine, host="192.168.1.20" if why == "not loopback" else "127.0.0.1")
    headers = {"X-Forwarded-For": "192.168.1.20"} if why == "came through nginx" else {}
    path = _path(secret="x" * 43) if why == "wrong secret" else _path()
    assert client.get(path, headers=headers).status_code == 404
    # the same request, from ffmpeg itself, is served -- so the 404 above is the guard's
    assert _client(engine).get(_path()).status_code == 206


@pytest.mark.parametrize(("ih", "idx"), [("cd" * 20, 0), (IH, 1), (IH.upper(), 0)])
def test_only_a_file_the_engine_holds_is_read(engine, ih, idx):
    assert _client(engine).get(_path(ih=ih, idx=idx)).status_code == 404


def test_a_torrent_without_metadata_is_not_read(tmp_path):
    class NoMetadata(Handle):
        def has_metadata(self):
            return False

    assert _client(Engine(tmp_path, NoMetadata())).get(_path()).status_code == 404


def test_it_reads_as_a_second_reader(engine, monkeypatch):
    """Uncounted, yielding to the viewer's deadlines, and with short piece waits."""
    seen = {}

    def fake_read(save_path, handle, idx, start, end, **kw):
        seen.update(kw)
        yield DATA[start:end + 1]

    monkeypatch.setattr(embedded_ass, "wait_and_read", fake_read)
    assert _client(engine).get(_path()).status_code == 206
    assert seen["count"] is False
    assert seen["yield_to_viewer"] is True
    assert (seen["first_timeout"], seen["timeout"]) == (20.0, 10.0)
    assert seen["info_hash"] == IH


def test_the_reader_url_carries_the_secret_and_the_api_port(engine):
    from stremiosrv.config import Settings
    app = create_app(settings=Settings(http_port=12345), engine=engine)

    class Req:
        pass

    req = Req()
    req.app = app
    assert embedded_ass.reader_url(req, IH, 2) == (
        f"http://127.0.0.1:12345/_embedded-ass-read/{embedded_ass._SECRET}/{IH}/2")
