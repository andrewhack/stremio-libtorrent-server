"""Build ffmpeg argv for a copy/transcode decision. Pure (returns list[str]) — unit-testable.

Mirrors the stock server's commands (audio -> AAC stereo; video copy with source keyframes).
Video transcode uses the active HW profile; for Pascal NVENC the 10-bit input is handled by the
CPU-scale path (no `-hwaccel_output_format cuda`), per the dual-GPU fork's NVIDIA-GPU.md.
"""
from __future__ import annotations


def build_audio_cmd(media_url: str, decision: dict, channels: int = 2) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-i", media_url, "-map", "a:0"]
    if decision.get("action") == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-ac", str(channels), "-ab", "384000", "-ar", "48000"]
    cmd += ["-f", "mp4", "pipe:1"]
    return cmd


def build_video_cmd(media_url: str, decision: dict, profile: str | None) -> list[str]:
    base = ["ffmpeg", "-hide_banner"]
    if decision.get("action") == "copy":
        return base + ["-i", media_url, "-map", "v:0",
                       "-c:v", "copy", "-force_key_frames:v", "source",
                       "-f", "mp4", "pipe:1"]

    w = decision.get("scale_width")
    if profile == "nvenc-linux":
        # NVDEC decode, CPU scale (Pascal-safe: 10-bit p010 -> 8-bit), NVENC encode
        cmd = base + ["-hwaccel", "cuda", "-i", media_url, "-map", "v:0"]
        vf = f"scale={w}:-2:flags=lanczos,format=yuv420p" if w else "format=yuv420p"
        cmd += ["-vf", vf, "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull"]
    elif profile and profile.startswith("vaapi"):
        cmd = base + ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                      "-i", media_url, "-map", "v:0"]
        vf = f"scale_vaapi=w={w}:h=-2" if w else "format=nv12|vaapi,hwupload"
        cmd += ["-vf", vf, "-c:v", "h264_vaapi"]
    else:  # CPU fallback
        cmd = base + ["-i", media_url, "-map", "v:0"]
        if w:
            cmd += ["-vf", f"scale={w}:-2:flags=lanczos"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast"]
    cmd += ["-f", "mp4", "pipe:1"]
    return cmd
