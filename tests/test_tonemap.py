"""Where an HDR source's tone map runs, and how startup decides the GPU can do it.

A transcode wrote HDR out as 8-bit SDR without converting its light levels: grey and flat. The GPU
tone-maps when it was shown to at startup -- an Intel GPU's tonemap_vaapi can run without error
and change nothing -- and the CPU only when the operator opted in, because on a small machine the
zscale chain runs well below realtime. Otherwise HDR is transcoded as before.
"""
from __future__ import annotations

import subprocess

import pytest

from stremiosrv.transcode import converter as converter_mod
from stremiosrv.transcode import profiler
from stremiosrv.transcode.converter import Converter

URL = "http://127.0.0.1:11470/aabbccddeeff00112233445566778899aabbccdd/0"
HDR = {"video": {"action": "transcode", "scale_width": 1920, "tonemap": True},
       "audio": {"action": "copy"}}
SDR = {"video": {"action": "transcode", "scale_width": 1920}, "audio": {"action": "copy"}}


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


def _vf(argv):
    return argv[argv.index("-vf") + 1]


@pytest.mark.parametrize("profile,gpu,cpu,expect", [
    ("vaapi-renderD128", True, False, "tonemap_vaapi"),
    ("vaapi-renderD128", True, True, "tonemap_vaapi"),   # the GPU's, when it has been shown to work
    ("vaapi-renderD128", False, True, "tonemap=tonemap=hable"),
    ("vaapi-renderD128", False, False, None),
    (None, False, True, "tonemap=tonemap=hable"),
    (None, True, False, None),                           # a GPU tone map needs the GPU profile
    ("nvenc-linux", False, True, "tonemap=tonemap=hable"),
])
def test_where_an_hdr_transcode_is_tone_mapped(tmp_path, started, profile, gpu, cpu, expect):
    conv = Converter(str(tmp_path), profile, gpu_tonemap=gpu, cpu_tonemap=cpu)
    conv.ensure_job("a", URL, HDR)
    vf = _vf(started[0].argv)
    if expect is None:
        assert "tonemap" not in vf
    else:
        assert expect in vf


def test_an_sdr_source_is_never_tone_mapped(tmp_path, started):
    conv = Converter(str(tmp_path), "vaapi-renderD128", gpu_tonemap=True, cpu_tonemap=True)
    conv.ensure_job("a", URL, SDR)
    assert "tonemap" not in _vf(started[0].argv)


@pytest.mark.parametrize("cpu,expect_tm", [(False, False), (True, True)])
def test_the_software_retry_of_a_gpu_tone_map_follows_the_cpu_setting(tmp_path, started, cpu,
                                                                     expect_tm):
    conv = Converter(str(tmp_path), "vaapi-renderD128", gpu_tonemap=True, cpu_tonemap=cpu)
    conv.ensure_job("a", URL, HDR)
    started[0].rc = 1
    assert conv.retry_in_software("a") is True
    assert "libx264" in started[1].argv
    assert ("tonemap=tonemap=hable" in _vf(started[1].argv)) is expect_tm


# --- startup: does this GPU's tonemap_vaapi change the picture?

def _frames(passthrough: bytes, tonemapped: bytes):
    """subprocess.run answering the two probe encodes with these Y planes, in call order."""
    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(argv)
        out = tonemapped if any("tonemap_vaapi" in a for a in argv) else passthrough
        return subprocess.CompletedProcess(argv, 0, out, b"")

    return run, calls


def test_a_gpu_whose_tone_map_changes_nothing_does_not_tone_map(monkeypatch):
    # What an older Intel GPU did: exit 0, the picture as it came in.
    plane = bytes([180] * 50 + [120] * 50)
    run, calls = _frames(plane, bytes([181] * 50 + [119] * 50))
    monkeypatch.setattr(profiler.subprocess, "run", run)
    assert profiler.gpu_tonemaps("/dev/dri/renderD128") is False
    assert any("vaapi=va:/dev/dri/renderD128" in " ".join(c) for c in calls)


def test_a_gpu_that_brings_hdr_highlights_up_to_white_tone_maps(monkeypatch):
    run, _ = _frames(bytes([180] * 50 + [120] * 50), bytes([232] * 50 + [110] * 50))
    monkeypatch.setattr(profiler.subprocess, "run", run)
    assert profiler.gpu_tonemaps("/dev/dri/renderD128") is True


def test_a_tone_map_that_fails_or_returns_nothing_does_not_count(monkeypatch):
    def failed(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    def empty(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def hangs(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 20)

    for run in (failed, empty, hangs):
        monkeypatch.setattr(profiler.subprocess, "run", run)
        assert profiler.gpu_tonemaps("/dev/dri/renderD128") is False


def test_only_a_vaapi_profile_has_its_tone_map_tested(monkeypatch):
    tried: list[str] = []
    monkeypatch.setattr(profiler, "gpu_tonemaps", lambda node: tried.append(node) or True)
    assert profiler.detect_gpu_tonemap(None) is False
    assert profiler.detect_gpu_tonemap("nvenc-linux") is False
    assert tried == []
    assert profiler.detect_gpu_tonemap("vaapi-renderD129") is True
    assert tried == ["/dev/dri/renderD129"]


def test_the_settings_default_to_testing_the_gpu_and_no_cpu_tone_map(monkeypatch):
    from stremiosrv.config import Settings

    s = Settings()
    assert s.transcode_gpu_tonemap is None and s.transcode_cpu_tonemap is False
    monkeypatch.setenv("STREMIOSRV_TRANSCODE_CPU_TONEMAP", "true")
    monkeypatch.setenv("STREMIOSRV_TRANSCODE_GPU_TONEMAP", "false")
    s = Settings()
    assert s.transcode_cpu_tonemap is True and s.transcode_gpu_tonemap is False
