"""Detect the active hardware-transcode profile at startup.

Mirrors the dual-GPU image's autodetect: NVIDIA present -> NVENC; else a VAAPI render node -> VAAPI;
else None (CPU/libx264 fallback). Pure-ish: only touches the environment, returns a string|None.
"""
from __future__ import annotations

import os
import shutil
import subprocess


def detect_profile() -> str | None:
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=5)
            if b"GPU" in r.stdout:
                return "nvenc-linux"
        except (OSError, subprocess.SubprocessError):
            pass
    if os.path.exists("/dev/dri/renderD128"):
        return "vaapi-renderD128"
    return None
