import re
import subprocess

from stremiosrv.transcode import profiler
from stremiosrv.transcode.profiler import detect_profile


def test_detect_returns_str_or_none():
    p = detect_profile()
    assert p is None or p == "nvenc-linux" or re.fullmatch(r"vaapi-renderD\d+", p)


# --- VAAPI: every render node, and only one that can encode H.264. Only renderD128 was looked at,
# and its existence was taken for an encoder: a box whose GPU sat at renderD129 transcoded on the CPU,
# and a GPU that could not encode H.264 failed every transcode.


def _no_nvidia(monkeypatch):
    monkeypatch.setattr(profiler.shutil, "which",
                        lambda name: None if name == "nvidia-smi" else f"/usr/bin/{name}")


def test_the_first_render_node_that_encodes_h264_is_used(monkeypatch):
    _no_nvidia(monkeypatch)
    monkeypatch.setattr(profiler, "_render_nodes",
                        lambda: ["/dev/dri/renderD128", "/dev/dri/renderD129"])
    tried = []

    def encodes(node):
        tried.append(node)
        return node.endswith("129")

    monkeypatch.setattr(profiler, "_encodes_h264", encodes)
    assert detect_profile() == "vaapi-renderD129"
    assert tried == ["/dev/dri/renderD128", "/dev/dri/renderD129"]


def test_no_render_node_that_encodes_means_the_cpu(monkeypatch):
    _no_nvidia(monkeypatch)
    monkeypatch.setattr(profiler, "_render_nodes", lambda: ["/dev/dri/renderD128"])
    monkeypatch.setattr(profiler, "_encodes_h264", lambda node: False)
    assert detect_profile() is None


def test_render_nodes_are_listed_in_number_order(tmp_path):
    for name in ("renderD129", "card0", "renderD128", "renderD130"):
        (tmp_path / name).touch()
    assert profiler._render_nodes(str(tmp_path)) == [
        str(tmp_path / "renderD128"), str(tmp_path / "renderD129"), str(tmp_path / "renderD130")]


def test_the_encode_check_is_a_real_h264_vaapi_encode_on_that_node(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(profiler.subprocess, "run", run)
    assert profiler._encodes_h264("/dev/dri/renderD129") is True
    argv = " ".join(seen[0])
    assert "vaapi=va:/dev/dri/renderD129" in argv
    assert "-c:v h264_vaapi" in argv


def test_a_node_whose_encode_fails_or_hangs_does_not_encode(monkeypatch):
    def failed(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    def hangs(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    def missing(argv, **kw):
        raise FileNotFoundError("ffmpeg")

    for run in (failed, hangs, missing):
        monkeypatch.setattr(profiler.subprocess, "run", run)
        assert profiler._encodes_h264("/dev/dri/renderD128") is False
