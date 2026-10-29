"""A hardware encode that fails before its first playlist is retried once in software.

The VAAPI branch assumed the GPU could encode H.264; one that could not -- or a driver that gave up
on a particular source -- failed the job, and the player got a 504 where the CPU would have played.
"""
from __future__ import annotations

import pytest

from stremiosrv.transcode import converter as converter_mod
from stremiosrv.transcode.converter import Converter

URL = "http://127.0.0.1:11470/aabbccddeeff00112233445566778899aabbccdd/0"
TRANSCODE = {"video": {"action": "transcode", "scale_width": 1920}, "audio": {"action": "copy"}}
COPY = {"video": {"action": "copy"}, "audio": {"action": "transcode"}}


class FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.rc: int | None = None

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = -15

    def kill(self):
        self.rc = -9

    def wait(self, timeout=None):
        return self.rc


@pytest.fixture
def started(monkeypatch):
    procs: list[FakeProc] = []

    def popen(argv, **k):
        procs.append(FakeProc(argv))
        return procs[-1]

    monkeypatch.setattr(converter_mod.subprocess, "Popen", popen)
    return procs


def test_an_early_hardware_failure_restarts_the_encode_in_software(tmp_path, started):
    conv = Converter(str(tmp_path), "vaapi-renderD128")
    conv.ensure_job("a", URL, TRANSCODE)
    assert "h264_vaapi" in started[0].argv
    started[0].rc = 1
    assert conv.retry_in_software("a") is True
    assert len(started) == 2
    assert "libx264" in started[1].argv and "-hwaccel" not in started[1].argv
    assert conv.active_count() == 1


def test_the_software_retry_happens_once(tmp_path, started):
    conv = Converter(str(tmp_path), "vaapi-renderD128")
    conv.ensure_job("a", URL, TRANSCODE)
    started[0].rc = 1
    conv.retry_in_software("a")
    started[1].rc = 1
    assert conv.retry_in_software("a") is False
    assert len(started) == 2


def test_an_encode_still_running_or_finished_is_left_alone(tmp_path, started):
    conv = Converter(str(tmp_path), "vaapi-renderD128")
    conv.ensure_job("a", URL, TRANSCODE)
    assert conv.retry_in_software("a") is False
    started[0].rc = 0
    assert conv.retry_in_software("a") is False
    assert len(started) == 1


def test_a_failure_that_was_not_on_hardware_is_not_retried(tmp_path, started):
    cpu = Converter(str(tmp_path / "cpu"), None)
    cpu.ensure_job("a", URL, TRANSCODE)
    started[-1].rc = 1
    assert cpu.retry_in_software("a") is False
    remux = Converter(str(tmp_path / "remux"), "vaapi-renderD128")
    remux.ensure_job("b", URL, COPY)  # the video is copied: no GPU in the command at all
    started[-1].rc = 1
    assert remux.retry_in_software("b") is False
    assert len(started) == 2


def test_a_joined_job_waiting_on_a_failed_encode_restarts_it(tmp_path, started):
    conv = Converter(str(tmp_path), "vaapi-renderD128")
    d = conv.ensure_job("a", URL, TRANSCODE, key="K")
    conv.ensure_job("b", URL, TRANSCODE, key="K")
    started[0].rc = 1
    assert conv.retry_in_software("b") is True
    assert len(started) == 2
    assert conv.job_file("b", "master.m3u8").parent == d


def test_the_hardware_attempts_log_is_kept_beside_the_retry(tmp_path, started):
    conv = Converter(str(tmp_path), "vaapi-renderD128")
    d = conv.ensure_job("a", URL, TRANSCODE)
    (d / "ffmpeg.log").write_bytes(b"Failed to initialise VAAPI connection\n")
    started[0].rc = 1
    conv.retry_in_software("a")
    assert (d / "ffmpeg-hw.log").read_bytes() == b"Failed to initialise VAAPI connection\n"


def test_the_master_route_retries_while_it_waits_for_the_first_playlist(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from stremiosrv.api import hls
    from stremiosrv.app import create_app

    monkeypatch.setattr(hls, "probe_media", lambda url: {
        "format": {"name": "matroska"}, "samples": {},
        "streams": [{"track": "video", "codec": "hevc", "width": 1920}]})
    monkeypatch.setattr(hls, "resolve_media_input", lambda request, url: url)
    retried: list[str] = []

    class Conv:
        def ensure_job(self, job_id, media_url, decision, key=None):
            return tmp_path

        def retry_in_software(self, job_id):
            retried.append(job_id)
            (tmp_path / "master.m3u8").write_text("#EXTM3U\n")  # the software encode starts
            return True

    app = create_app()
    app.state.converter = Conv()
    r = TestClient(app).get("/hlsv2/job1/master.m3u8",
                            params={"mediaURL": URL, "videoCodecs": "h264"})
    assert r.status_code == 200
    assert retried[0] == "job1"
