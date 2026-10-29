"""Detect the active hardware-transcode profile at startup.

Mirrors the dual-GPU image's autodetect: NVIDIA present -> NVENC; else the first VAAPI render node
that can encode H.264 -> VAAPI on that node; else None (CPU/libx264 fallback). Pure-ish: only
touches the environment, returns a string|None.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess


def _render_nodes(dri: str = "/dev/dri") -> list[str]:
    """Every DRM render node, in number order: renderD128 is the first GPU, renderD129 the next."""
    nodes = [p for p in glob.glob(os.path.join(dri, "renderD*"))
             if re.fullmatch(r"renderD\d+", os.path.basename(p))]
    return sorted(nodes, key=lambda p: int(os.path.basename(p)[len("renderD"):]))


def _encodes_h264(node: str, timeout: float = 20) -> bool:
    """Whether this node can encode H.264, shown by encoding a few frames on it -- the only thing a
    transcode asks of it that software cannot stand in for. A node that exists is not one that
    encodes: some GPUs decode only, and a node can belong to a device with no VAAPI driver."""
    argv = ["ffmpeg", "-hide_banner", "-v", "error",
            "-init_hw_device", f"vaapi=va:{node}", "-filter_hw_device", "va",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-frames:v", "5",
            "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-f", "null", "-"]
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_profile() -> str | None:
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=5)
            if b"GPU" in r.stdout:
                return "nvenc-linux"
        except (OSError, subprocess.SubprocessError):
            pass
    for node in _render_nodes():
        if _encodes_h264(node):
            print(f"[transcode] VAAPI: {node} encodes H.264, using it", flush=True)
            return f"vaapi-{os.path.basename(node)}"
        print(f"[transcode] VAAPI: {node} cannot encode H.264, skipped", flush=True)
    return None
