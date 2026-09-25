"""The whole path a TV's styled-subtitle requests take, over real HTTP with the real ffmpeg:

    route -> ffprobe / ffmpeg -> the private reader, through uvicorn -> the torrent's file on disk

The route tests fake ffmpeg and the reader tests fake the route; only this proves the pieces fit.
A fake engine stands in for libtorrent: one torrent, every piece of the fixture present.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
import uvicorn
from embedded_ass_fixture import build, needs_ffmpeg

from stremiosrv import metrics
from stremiosrv.api import embedded_ass
from stremiosrv.app import create_app
from stremiosrv.config import Settings

pytestmark = needs_ffmpeg

IH = "ab" * 20
PIECE = 64 * 1024


class Handle:
    def __init__(self, path):
        self.path = path

    def has_metadata(self):
        return True

    def num_files(self):
        return 1

    def file_size(self, idx):
        return self.path.stat().st_size

    def file_path(self, idx):
        return self.path.name

    def piece_length(self):
        return PIECE

    def file_offset(self, idx):
        return 0

    def num_pieces(self):
        return -(-self.file_size(0) // PIECE)

    def have_piece(self, i):
        return True

    def boost_piece(self, p, ms, keep_existing=False):
        pass


class Engine:
    def __init__(self, path):
        self.handle, self.root = Handle(path), str(path.parent)

    def get(self, info_hash):
        return self.handle if info_hash == IH else None

    def save_path(self):
        return self.root


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    fx = build(tmp_path_factory.mktemp("ass-int"))
    port = _free_port()
    app = create_app(settings=Settings(http_port=port), engine=Engine(fx["path"]))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    yield {"base": f"http://127.0.0.1:{port}", "attachments": fx["attachments"]}
    srv.should_exit = True
    t.join(10)


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(embedded_ass, "FONT_ROOT", str(tmp_path / "fonts"))
    embedded_ass.reset()
    metrics.reset()
    yield
    embedded_ass.reset()
    metrics.reset()


def _get(server, path, **query):
    q = {"mediaURL": f"https://tv.example:12470/{IH}/0", **query}
    url = f"{server['base']}{path}?{urllib.parse.urlencode(q)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.status, r.headers.get("content-type"), r.read()


def test_discovery_over_the_real_path(server):
    status, ctype, body = _get(server, "/embedded-ass")
    assert (status, ctype) == (200, "application/json")
    assert json.loads(body) == {
        "tracks": [{"number": 1, "codec": "ass", "lang": "eng", "label": "Signs (styled)"}],
        "fonts": [{"id": 3}, {"id": 4}],
    }


def test_a_window_over_the_real_path(server):
    status, ctype, body = _get(server, "/embedded-ass/1.ass", **{"from": "30000", "to": "60000"})
    assert (status, ctype) == (200, "text/x-ssa; charset=utf-8")
    text = body.decode("utf-8")
    assert "[Script Info]" in text and "Style: Default,Fixture Sans" in text
    starts = {int(m) * 60 + int(s) for m, s in
              re.findall(r"^Dialogue: \d+,0:(\d\d):(\d\d)\.\d\d,", text, re.MULTILINE)}
    assert starts == set(range(20, 60, 2))
    assert metrics.playback_stats()["embeddedAssWindows"] == 1


def test_fonts_over_the_real_path(server):
    for i, ctype in ((3, "application/x-truetype-font"), (4, "application/octet-stream")):
        status, got_type, body = _get(server, f"/embedded-ass/font/{i}")
        assert (status, got_type) == (200, ctype)
        assert body == server["attachments"][i]


def test_the_reader_refuses_a_request_without_the_secret(server):
    url = f"{server['base']}/_embedded-ass-read/nope/{IH}/0"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url, timeout=10)
    assert e.value.code == 404
