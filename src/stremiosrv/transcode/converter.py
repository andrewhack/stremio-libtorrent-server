"""On-the-fly HLS transcoder: one ffmpeg job per request id, fMP4 segments on disk.

`build_hls_cmd` is pure (unit-testable); `Converter` manages the ffmpeg subprocess lifecycle.
Uses an EVENT playlist so the player can start after the first segment instead of waiting for the
whole transcode (full VOD-on-demand seeking is a later refinement).
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path


def build_hls_cmd(media_url: str, decision: dict, profile: str | None, out_dir: str | Path) -> list[str]:
    out_dir = str(out_dir)
    v = decision.get("video", {})
    a = decision.get("audio")
    argv = ["ffmpeg", "-hide_banner", "-y"]

    if v.get("action") == "transcode":
        if profile == "nvenc-linux":
            argv += ["-hwaccel", "cuda"]
        elif profile and profile.startswith("vaapi"):
            argv += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]

    argv += ["-i", media_url, "-map", "0:v:0"]
    if a is not None:
        argv += ["-map", "0:a:0?"]

    # Video
    if v.get("action") == "copy":
        argv += ["-c:v", "copy"]
    else:
        w = v.get("scale_width")
        if profile == "nvenc-linux":
            argv += ["-vf", f"scale={w}:-2:flags=lanczos,format=yuv420p" if w else "format=yuv420p",
                     "-c:v", "h264_nvenc", "-preset", "p4"]
        elif profile and profile.startswith("vaapi"):
            if w:
                argv += ["-vf", f"scale_vaapi=w={w}:h=-2"]
            argv += ["-c:v", "h264_vaapi"]
        else:
            if w:
                argv += ["-vf", f"scale={w}:-2:flags=lanczos"]
            argv += ["-c:v", "libx264", "-preset", "veryfast"]

    # Audio
    if a is not None:
        if a.get("action") == "copy":
            argv += ["-c:a", "copy"]
        else:
            argv += ["-c:a", "aac", "-ac", "2", "-ab", "384000"]

    argv += [
        "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
        "-hls_segment_type", "fmp4", "-hls_flags", "independent_segments",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", f"{out_dir}/seg%d.m4s",
        "-master_pl_name", "master.m3u8", f"{out_dir}/index.m3u8",
    ]
    return argv


class Converter:
    def __init__(self, cache_root: str, profile: str | None):
        self.base = Path(cache_root) / "transcode"
        self.profile = profile
        self._jobs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def job_dir(self, job_id: str) -> Path:
        return self.base / job_id

    def ensure_job(self, job_id: str, media_url: str, decision: dict) -> Path:
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None and existing.poll() is None:
                return self.job_dir(job_id)
            d = self.job_dir(job_id)
            d.mkdir(parents=True, exist_ok=True)
            argv = build_hls_cmd(media_url, decision, self.profile, d)
            log = open(d / "ffmpeg.log", "wb")
            self._jobs[job_id] = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=log)
            return d

    def active_count(self) -> int:
        """Number of ffmpeg transcode jobs currently running (for the admin transcode/GPU card)."""
        with self._lock:
            return sum(1 for p in self._jobs.values() if p.poll() is None)

    def stop(self, job_id: str) -> None:
        with self._lock:
            p = self._jobs.pop(job_id, None)
        if p is not None and p.poll() is None:
            p.terminate()

    def stop_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for p in jobs:
            if p.poll() is None:
                p.terminate()
