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
import tempfile


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


# An HDR10 frame like a real source's: a test pattern whose white is coded at 1000 nits (PQ, BT.2020,
# 10-bit HEVC) with the mastering metadata a GPU's tone map reads.
_HDR10_FRAME = [
    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=1", "-frames:v", "1",
    "-vf", "zscale=tin=bt709:pin=bt709:min=bt709:t=smpte2084:p=bt2020:m=bt2020nc:r=tv:npl=1000,"
           "format=yuv420p10le",
    "-c:v", "libx265", "-x265-params",
    "log-level=error:pools=1:frame-threads=1:hdr10=1:colorprim=bt2020:transfer=smpte2084:"
    "colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
    "WP(15635,16450)L(10000000,1):max-cll=1000,400",
    "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
]


def _ran(argv: list[str], timeout: float) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _luma(argv: list[str], timeout: float) -> bytes:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return b""
    return r.stdout if r.returncode == 0 else b""


def gpu_tonemaps(node: str, timeout: float = 20) -> bool:
    """Whether this node's tonemap_vaapi really converts HDR to SDR.

    Running without an error proves nothing: an older Intel GPU passes the frame through unchanged.
    So one HDR10 frame goes through the GPU twice, once only converted to 8-bit and once tone-mapped,
    and the tone map counts only if it changes the picture: highlights coded at 1000 nits come out
    near white instead of grey."""
    with tempfile.TemporaryDirectory() as tmp:
        frame = os.path.join(tmp, "hdr10.mkv")
        if not _ran(["ffmpeg", "-hide_banner", "-v", "error", "-y", *_HDR10_FRAME, frame], timeout):
            return False

        def plane(vpp: str) -> bytes:
            return _luma(["ffmpeg", "-hide_banner", "-v", "error",
                          "-init_hw_device", f"vaapi=va:{node}", "-filter_hw_device", "va",
                          "-i", frame, "-frames:v", "1",
                          "-vf", f"format=p010,hwupload,{vpp},hwdownload,format=nv12,"
                                 "extractplanes=y",
                          "-f", "rawvideo", "pipe:1"], timeout)

        plain = plane("scale_vaapi=format=nv12")
        mapped = plane("tonemap_vaapi=format=nv12:t=bt709:m=bt709:p=bt709")
    if not plain or len(plain) != len(mapped):
        return False
    mean = abs(sum(mapped) - sum(plain)) / len(plain)
    return mean >= 8 or abs(max(mapped) - max(plain)) >= 15


def detect_gpu_tonemap(profile: str | None) -> bool:
    """Whether HDR transcodes can be tone-mapped on the GPU of this profile; printed at startup."""
    if not profile or not profile.startswith("vaapi-"):
        return False
    node = f"/dev/dri/{profile.split('-', 1)[1]}"
    if gpu_tonemaps(node):
        print(f"[transcode] VAAPI: {node} tone-maps HDR to SDR, using it", flush=True)
        return True
    print(f"[transcode] VAAPI: {node} does not tone-map HDR to SDR; HDR transcodes are tone-mapped"
          " only with STREMIOSRV_TRANSCODE_CPU_TONEMAP=true", flush=True)
    return False
