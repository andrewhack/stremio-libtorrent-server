"""Two players on one file share one encode.

Each playback attempt carries a fresh job id, so a reload or a second device on the same file
started a second full ffmpeg, and the first one ran on until the idle reaper ended it. A job now
joins a running (or cleanly finished) encode of the identical workload -- same media, same
copy/transcode decision -- and the encode lives until its last reader leaves or goes idle.
"""
from __future__ import annotations

import pytest

from stremiosrv.transcode import converter as converter_mod
from stremiosrv.transcode.converter import Converter

DEC = {"video": {"action": "transcode", "scale_width": 1920}, "audio": {"action": "copy"}}
URL = "http://127.0.0.1:11470/aabbccddeeff00112233445566778899aabbccdd/0"


class FakeProc:
    """subprocess.Popen as the converter uses it: poll() is None while running, else the exit code."""

    def __init__(self, *a, **k):
        self.rc: int | None = None
        self.terminated = False

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = -15

    def kill(self):
        self.rc = -9

    def wait(self, timeout=None):
        return self.rc


@pytest.fixture
def started(monkeypatch):
    """Every ffmpeg the converter starts, in order."""
    procs: list[FakeProc] = []

    def popen(*a, **k):
        procs.append(FakeProc())
        return procs[-1]

    monkeypatch.setattr(converter_mod.subprocess, "Popen", popen)
    return procs


def test_a_second_player_on_the_same_workload_joins_the_running_encode(tmp_path, started):
    conv = Converter(str(tmp_path), None)
    da = conv.ensure_job("a", URL, DEC, key="K")
    db = conv.ensure_job("b", URL, DEC, key="K")
    assert len(started) == 1
    assert db == da
    assert conv.job_file("b", "seg0.m4s") == conv.job_file("a", "seg0.m4s")


def test_a_different_workload_gets_its_own_encode(tmp_path, started):
    conv = Converter(str(tmp_path), None)
    conv.ensure_job("a", URL, DEC, key="K")
    conv.ensure_job("b", URL, DEC, key="K2")
    conv.ensure_job("c", URL, DEC)  # no key: never shared
    assert len(started) == 3


def test_destroying_one_reader_leaves_the_encode_to_the_other(tmp_path, started):
    conv = Converter(str(tmp_path), None)
    d = conv.ensure_job("a", URL, DEC, key="K")
    conv.ensure_job("b", URL, DEC, key="K")
    conv.stop("a")
    assert not started[0].terminated and d.exists()
    conv.stop("b")
    assert started[0].terminated and not d.exists()


def test_the_joiner_leaving_first_leaves_the_encode_to_the_starter(tmp_path, started):
    conv = Converter(str(tmp_path), None)
    d = conv.ensure_job("a", URL, DEC, key="K")
    conv.ensure_job("b", URL, DEC, key="K")
    conv.stop("b")
    assert not started[0].terminated and d.exists()
    # b is gone, so its id no longer names the shared directory: the route answers it 404
    assert conv.job_file("b", "seg0.m4s").parent.name == "b"


def test_a_shared_encode_is_reaped_only_when_every_reader_is_idle(tmp_path, started):
    conv = Converter(str(tmp_path), None)
    d = conv.ensure_job("a", URL, DEC, key="K")
    conv.ensure_job("b", URL, DEC, key="K")
    conv._seen["a"] -= 600  # the player that started it went away without /destroy
    assert conv.reap_idle(300) == []
    assert not started[0].terminated
    conv._seen["b"] -= 600
    assert conv.reap_idle(300) == ["a"]
    assert started[0].terminated and not d.exists()


def test_a_joined_reader_keeps_a_finished_encode_from_the_sweep(tmp_path, started):
    conv = Converter(str(tmp_path), None)
    d = conv.ensure_job("a", URL, DEC, key="K")
    started[0].rc = 0  # the whole file is transcoded
    conv.ensure_job("b", URL, DEC, key="K")
    assert len(started) == 1  # a clean, finished encode is joined, not redone
    conv._seen["a"] -= 3600
    conv.touch("b")
    assert conv.sweep(max_age=600) == 0
    assert d.exists()


def test_a_finished_encode_is_not_restarted_under_its_readers(tmp_path, started):
    # The job that started it asks for its playlist again after ffmpeg finished: restarting it
    # would rewrite, from the start, the output the joined player is reading.
    conv = Converter(str(tmp_path), None)
    conv.ensure_job("a", URL, DEC, key="K")
    conv.ensure_job("b", URL, DEC, key="K")
    started[0].rc = 0
    conv.ensure_job("a", URL, DEC, key="K")
    assert len(started) == 1


def test_a_failed_encode_is_not_joined(tmp_path, started):
    conv = Converter(str(tmp_path), None)
    conv.ensure_job("a", URL, DEC, key="K")
    started[0].rc = 1
    conv.ensure_job("b", URL, DEC, key="K")
    assert len(started) == 2
    assert conv.job_file("b", "seg0.m4s").parent.name == "b"


# --- the route: which requests count as the same workload ---

IH = "aabbccddeeff00112233445566778899aabbccdd"
_PROBE = {"format": {"name": "matroska"}, "samples": {},
          "streams": [{"track": "video", "codec": "hevc", "width": 3840},
                      {"track": "audio", "codec": "aac", "channels": 2}]}


def _keys_for(monkeypatch, tmp_path, requests: list[dict]) -> list[str | None]:
    """The workload key the master route hands the converter for each request."""
    from fastapi.testclient import TestClient

    from stremiosrv.api import hls
    from stremiosrv.app import create_app

    monkeypatch.setattr(hls, "probe_media", lambda url: _PROBE)
    monkeypatch.setattr(hls, "resolve_media_input", lambda request, url: url + "#resolved")
    keys: list[str | None] = []

    class Conv:
        def ensure_job(self, job_id, media_url, decision, key=None):
            keys.append(key)
            (tmp_path / "master.m3u8").write_text("#EXTM3U\n")
            return tmp_path

    app = create_app()
    app.state.converter = Conv()
    c = TestClient(app)
    for n, params in enumerate(requests):
        assert c.get(f"/hlsv2/job{n}/master.m3u8", params=params).status_code == 200
    return keys


def test_one_torrent_file_is_one_workload_whatever_url_reached_it(monkeypatch, tmp_path):
    k = _keys_for(monkeypatch, tmp_path, [
        {"mediaURL": f"http://127.0.0.1:11470/{IH}/0", "videoCodecs": "h264"},
        {"mediaURL": f"http://127.0.0.1:12470/{IH.upper()}/0?tr=udp%3A%2F%2Ft.example%3A1",
         "videoCodecs": "h264"},
    ])
    assert k[0] is not None and k[0] == k[1]


def test_another_file_or_another_decision_is_another_workload(monkeypatch, tmp_path):
    k = _keys_for(monkeypatch, tmp_path, [
        {"mediaURL": f"http://127.0.0.1:11470/{IH}/0", "videoCodecs": "h264"},
        {"mediaURL": f"http://127.0.0.1:11470/{IH}/1", "videoCodecs": "h264"},
        {"mediaURL": f"http://127.0.0.1:11470/{IH}/0", "videoCodecs": "h264", "maxWidth": 1280},
    ])
    assert len(set(k)) == 3


def test_an_external_source_is_keyed_by_the_url_the_player_sent(monkeypatch, tmp_path):
    # Not by the resolved reader URL, which carries a fresh ticket on every request.
    k = _keys_for(monkeypatch, tmp_path, [
        {"mediaURL": "https://cdn.example/v.mkv", "videoCodecs": "h264"},
        {"mediaURL": "https://cdn.example/v.mkv", "videoCodecs": "h264"},
        {"mediaURL": "https://cdn.example/w.mkv", "videoCodecs": "h264"},
    ])
    assert k[0] == k[1] != k[2]
    assert "#resolved" not in k[0]
