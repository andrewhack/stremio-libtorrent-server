"""The three routes a TV calls for styled embedded subtitles, with ffprobe and ffmpeg faked.

The contract is stremio-video 0.0.98's (withStreamingServer.js, withHTMLSubtitles.js). What the
real ffmpeg returns is tested in test_embedded_ass.py and, end to end over HTTP, in
test_embedded_ass_int.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from stremiosrv import metrics
from stremiosrv.api import embedded_ass
from stremiosrv.app import create_app

IH, IH2 = "ab" * 20, "cd" * 20
PROBE = {"streams": [
    {"index": 0, "codec_type": "video", "codec_name": "h264"},
    {"index": 1, "codec_type": "subtitle", "codec_name": "ass",
     "tags": {"language": "eng", "title": "Signs"}},
    {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    {"index": 3, "codec_type": "attachment", "codec_name": "ttf",
     "tags": {"filename": "a.ttf", "mimetype": "application/x-truetype-font"}},
    {"index": 4, "codec_type": "attachment", "codec_name": "otf",
     "tags": {"filename": "b.otf", "mimetype": "application/vnd.ms-opentype"}},
]}
ASS = b"[Script Info]\n...\n[Events]\nDialogue: 0,0:00:30.00,0:00:31.50,Default,,0,0,0,,x\n"


class Handle:
    def has_metadata(self):
        return True

    def num_files(self):
        return 1


class Engine:
    def get(self, info_hash):
        return Handle() if info_hash in (IH, IH2) else None

    def add(self, *a, **k):
        raise AssertionError("an embedded-ASS route must never add a torrent")


class Tools:
    """Stands in for subprocess.run: records every argv, answers like ffprobe / ffmpeg would."""

    def __init__(self):
        self.runs: list[list[str]] = []
        self.probe_rc, self.ffmpeg_rc = 0, 0
        self.timeout = None  # "ffprobe" or "ffmpeg": that tool times out
        self.dump_delay = 0.0  # seconds a font dump takes

    def __call__(self, argv, capture_output=True, timeout=None):
        self.runs.append(argv)
        if argv[0] == self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        if argv[0] == "ffprobe":
            return subprocess.CompletedProcess(argv, self.probe_rc, json.dumps(PROBE).encode(), b"")
        if any(a.startswith("-dump_attachment:") for a in argv):
            time.sleep(self.dump_delay)
            for i, a in enumerate(argv):
                if a.startswith("-dump_attachment:"):
                    with open(argv[i + 1], "wb") as f:
                        f.write(f"font {a.split(':')[1]}".encode())
            return subprocess.CompletedProcess(argv, self.ffmpeg_rc, b"", b"")
        return subprocess.CompletedProcess(argv, self.ffmpeg_rc, ASS, b"")

    def of(self, tool):
        return [a for a in self.runs if a[0] == tool]


@pytest.fixture
def tools(monkeypatch, tmp_path):
    t = Tools()
    monkeypatch.setattr(embedded_ass.subprocess, "run", t)
    monkeypatch.setattr(embedded_ass, "FONT_ROOT", str(tmp_path / "fonts"))
    embedded_ass.reset()
    metrics.reset()
    yield t
    embedded_ass.reset()
    metrics.reset()


@pytest.fixture
def client():
    return TestClient(create_app(engine=Engine()))


def _media(ih=IH, idx=0):
    return {"mediaURL": f"https://tv.example:12470/{ih}/{idx}"}


def test_discovery_answers_the_file_s_ass_tracks_and_fonts(client, tools):
    r = client.get("/embedded-ass", params=_media())
    assert r.status_code == 200
    assert r.json() == {
        "tracks": [{"number": 1, "codec": "ass", "lang": "eng", "label": "Signs (styled)"}],
        "fonts": [{"id": 3}, {"id": 4}],
    }
    assert metrics.playback_stats()["embeddedAssAsks"] == 1
    [probe] = tools.of("ffprobe")
    # ffprobe reads the private reader -- never the URL the client sent
    assert probe[-1] == f"http://127.0.0.1:11470/_embedded-ass-read/{embedded_ass._SECRET}/{IH}/0"


def test_the_counters_reach_stats_json(client, tools):
    client.get("/embedded-ass", params=_media())
    playback = client.get("/stats.json").json()["playback"]
    assert (playback["embeddedAssAsks"], playback["embeddedAssWindows"],
            playback["embeddedAssFailures"]) == (1, 0, 0)


@pytest.mark.parametrize("media", [
    {"mediaURL": "https://cdn.example/movie.mkv"},  # not a torrent stream at all
    {"mediaURL": ""},
    {},                                              # no mediaURL
    _media(ih="ef" * 20),                            # a torrent the engine does not hold
    _media(idx=1),                                   # a file the torrent does not have
])
def test_anything_but_a_torrent_this_server_plays_is_404_and_never_probed(client, tools, media):
    assert client.get("/embedded-ass", params=media).status_code == 404
    assert client.get("/embedded-ass/1.ass",
                      params={**media, "from": "0", "to": "60000"}).status_code == 404
    assert client.get("/embedded-ass/font/3", params=media).status_code == 404
    assert tools.runs == []
    assert metrics.playback_stats()["embeddedAssAsks"] == 0


def test_a_file_is_probed_once_whatever_is_asked(client, tools):
    client.get("/embedded-ass", params=_media())
    client.get("/embedded-ass", params=_media())
    client.get("/embedded-ass/1.ass", params={**_media(), "from": "0", "to": "60000"})
    client.get("/embedded-ass/font/3", params=_media())
    assert len(tools.of("ffprobe")) == 1


@pytest.mark.parametrize(("setup", "status"), [("timeout", 504), ("rc", 502)])
def test_a_probe_that_fails_is_a_counted_5xx(client, tools, setup, status):
    if setup == "timeout":
        tools.timeout = "ffprobe"
    else:
        tools.probe_rc = 1
    assert client.get("/embedded-ass", params=_media()).status_code == status
    assert metrics.playback_stats()["embeddedAssFailures"] == 1
    assert metrics.playback_stats()["embeddedAssAsks"] == 0


def test_a_window_is_ffmpeg_s_output_as_ass(client, tools):
    r = client.get("/embedded-ass/1.ass", params={**_media(), "from": "30000", "to": "60000"})
    assert r.status_code == 200
    assert r.content == ASS
    assert r.headers["content-type"] == "text/x-ssa; charset=utf-8"
    [argv] = tools.of("ffmpeg")
    assert argv[argv.index("-i") + 1].startswith("http://127.0.0.1:11470/_embedded-ass-read/")
    assert argv[argv.index("-map") + 1] == "0:1"
    assert (argv[argv.index("-ss") + 1], argv[argv.index("-to") + 1]) == ("20.000", "60.000")
    assert metrics.playback_stats()["embeddedAssWindows"] == 1


@pytest.mark.parametrize("window", [
    {"to": "60000"}, {"from": "0"}, {"from": "a", "to": "60000"},
    {"from": "60000", "to": "0"}, {"from": "0", "to": "300001"},
])
def test_a_window_that_is_not_one_is_400_and_runs_nothing(client, tools, window):
    assert client.get("/embedded-ass/1.ass", params={**_media(), **window}).status_code == 400
    assert tools.runs == []


@pytest.mark.parametrize("number", [2, 3, 9])  # a SubRip track, a font, no stream at all
def test_only_an_ass_track_is_extracted(client, tools, number):
    params = {**_media(), "from": "0", "to": "60000"}
    assert client.get(f"/embedded-ass/{number}.ass", params=params).status_code == 404
    assert tools.of("ffmpeg") == []


@pytest.mark.parametrize(("setup", "status"), [("timeout", 504), ("rc", 502)])
def test_an_extraction_that_fails_is_a_counted_5xx(client, tools, setup, status):
    if setup == "timeout":
        tools.timeout = "ffmpeg"
    else:
        tools.ffmpeg_rc = 1
    params = {**_media(), "from": "0", "to": "60000"}
    assert client.get("/embedded-ass/1.ass", params=params).status_code == status
    assert metrics.playback_stats()["embeddedAssFailures"] == 1
    assert metrics.playback_stats()["embeddedAssWindows"] == 0


def test_a_third_extraction_waits_then_is_503(client, tools, monkeypatch):
    busy = threading.BoundedSemaphore(2)
    busy.acquire()
    busy.acquire()
    monkeypatch.setattr(embedded_ass, "_WORK", busy)
    monkeypatch.setattr(embedded_ass, "_WORK_WAIT", 0.05)
    params = {**_media(), "from": "0", "to": "60000"}
    assert client.get("/embedded-ass/1.ass", params=params).status_code == 503
    assert tools.of("ffmpeg") == []
    assert metrics.playback_stats()["embeddedAssFailures"] == 1


def test_fonts_are_dumped_once_and_served_as_their_type(client, tools):
    a = client.get("/embedded-ass/font/3", params=_media())
    b = client.get("/embedded-ass/font/4", params=_media())
    assert (a.status_code, a.content) == (200, b"font 3")
    assert a.headers["content-type"] == "application/x-truetype-font"
    assert (b.status_code, b.content) == (200, b"font 4")
    assert b.headers["content-type"] == "application/octet-stream"
    [dump] = tools.of("ffmpeg")
    assert dump[dump.index("-dump_attachment:3") + 1].endswith(os.path.join(f"{IH}-0", "3.font"))


@pytest.mark.parametrize("font_id", [1, 2, 9])  # an ASS track, a SubRip track, nothing
def test_only_a_font_attachment_is_served(client, tools, font_id):
    assert client.get(f"/embedded-ass/font/{font_id}", params=_media()).status_code == 404
    assert tools.of("ffmpeg") == []


def test_only_the_last_files_fonts_stay_on_disk(client, tools, monkeypatch, tmp_path):
    monkeypatch.setattr(embedded_ass, "_FONTS_KEEP", 1)
    client.get("/embedded-ass/font/3", params=_media(IH))
    assert (tmp_path / "fonts" / f"{IH}-0" / "3.font").exists()
    client.get("/embedded-ass/font/3", params=_media(IH2))
    assert not (tmp_path / "fonts" / f"{IH}-0").exists()
    assert (tmp_path / "fonts" / f"{IH2}-0" / "3.font").exists()


def test_the_font_cache_starts_empty_in_each_process(client, tools, tmp_path):
    stale = tmp_path / "fonts" / f"{IH}-0"
    stale.mkdir(parents=True)
    (stale / "3.font").write_bytes(b"left by an older process")
    (stale / ".done").write_bytes(b"")
    r = client.get("/embedded-ass/font/3", params=_media())
    assert r.content == b"font 3"
    assert len(tools.of("ffmpeg")) == 1


def test_a_window_never_waits_behind_a_font_dump(client, tools):
    """The TV fetches windows while its fonts are still being dumped, and a dump holds the file's
    lock. A window for a file whose track list is known must not queue behind it."""
    client.get("/embedded-ass", params=_media())  # the track list is now known
    tools.dump_delay = 1.5
    font = threading.Thread(target=client.get, args=("/embedded-ass/font/3",),
                            kwargs={"params": _media()})
    font.start()
    time.sleep(0.3)  # the dump is running, holding the file lock
    started = time.monotonic()
    r = client.get("/embedded-ass/1.ass", params={**_media(), "from": "0", "to": "60000"})
    took = time.monotonic() - started
    font.join()
    assert r.status_code == 200
    assert took < 0.8, f"the window waited {took:.1f} s behind the font dump"
