#!/usr/bin/env python3
"""Fork-owned FFmpeg policy wrapper.

The Stremio core keeps invoking ``ffmpeg`` normally. This wrapper sits earlier
in PATH, reads the WebAdmin configuration, and only changes explicit stream
copy decisions when policy requires transcoding. Existing upstream transcode
commands are left untouched.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "transcoding_mode": "auto",
    "transcoding_hwaccel": "vaapi",
    "transcoding_vaapi_device": "/dev/dri/renderD128",
    "transcoding_video_codec": "h264_vaapi",
    "transcoding_video_quality": 22,
    "transcoding_audio_codec": "aac",
    "transcoding_audio_bitrate": "192k",
    "transcoding_copy_video": True,
    "transcoding_copy_audio": True,
    "transcoding_direct_video_codecs": "h264",
    "transcoding_direct_audio_codecs": "aac,mp3,ac3",
    "transcoding_fallback_codec": "libx264",
    "transcoding_hw_decode": True,
}

ENV_MAP = {
    "transcoding_mode": "TRANSCODING_MODE",
    "transcoding_hwaccel": "TRANSCODING_HWACCEL",
    "transcoding_vaapi_device": "VAAPI_DEVICE",
    "transcoding_video_codec": "TRANSCODING_VIDEO_CODEC",
    "transcoding_video_quality": "TRANSCODING_VIDEO_QUALITY",
    "transcoding_audio_codec": "TRANSCODING_AUDIO_CODEC",
    "transcoding_audio_bitrate": "TRANSCODING_AUDIO_BITRATE",
    "transcoding_copy_video": "TRANSCODING_COPY_VIDEO",
    "transcoding_copy_audio": "TRANSCODING_COPY_AUDIO",
    "transcoding_direct_video_codecs": "TRANSCODING_DIRECT_VIDEO_CODECS",
    "transcoding_direct_audio_codecs": "TRANSCODING_DIRECT_AUDIO_CODECS",
    "transcoding_fallback_codec": "TRANSCODING_FALLBACK_CODEC",
    "transcoding_hw_decode": "TRANSCODING_HW_DECODE",
}

REAL_FFMPEG = os.getenv("FFMPEG_REAL", "/usr/local/libexec/stremio/ffmpeg-real")
REAL_FFPROBE = os.getenv("FFPROBE_REAL", "/usr/local/libexec/stremio/ffprobe-real")
CONFIG_FILE = os.getenv("STREMIOSRV_EXTERNAL_CONFIG", "/config/admin-settings.json")


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normalise_setting(name: str, value: object) -> object:
    if name in {"transcoding_copy_video", "transcoding_copy_audio", "transcoding_hw_decode"}:
        return _bool(value, bool(DEFAULTS[name]))
    if name == "transcoding_video_quality":
        try:
            return max(0, min(51, int(value)))
        except (TypeError, ValueError):
            return DEFAULTS[name]
    return str(value).strip() if value is not None else DEFAULTS[name]


def load_settings() -> dict[str, object]:
    """Load built-ins, then Compose/.env, then persisted WebAdmin settings.

    Persisted WebAdmin values intentionally win over environment defaults so a
    setting changed in the UI remains effective after a container restart.
    """
    settings = dict(DEFAULTS)
    for name, env_name in ENV_MAP.items():
        if env_name in os.environ:
            settings[name] = _normalise_setting(name, os.environ[env_name])

    try:
        raw = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for name in DEFAULTS:
                if name in raw:
                    settings[name] = _normalise_setting(name, raw[name])
    except (OSError, ValueError, TypeError):
        pass
    return settings


def _csv(value: object) -> set[str]:
    return {x.strip().lower() for x in str(value or "").split(",") if x.strip()}


def _input_from_args(args: list[str]) -> str | None:
    for index, arg in enumerate(args[:-1]):
        if arg == "-i":
            return args[index + 1]
    return None


def probe_media(media_url: str) -> dict[str, str | None] | None:
    cmd = [
        REAL_FFPROBE,
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "json",
        media_url,
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=True)
        payload = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    result: dict[str, str | None] = {"video": None, "audio": None}
    for stream in payload.get("streams", []):
        kind = str(stream.get("codec_type", "")).lower()
        codec = str(stream.get("codec_name", "")).lower() or None
        if kind in result and result[kind] is None:
            result[kind] = codec
    return result


def available_encoders() -> set[str]:
    try:
        completed = subprocess.run(
            [REAL_FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(re.findall(r"^\s*[VAS\.]{6}\s+([\w-]+)", completed.stdout, re.MULTILINE))


def _has_copy(args: list[str], media_type: str) -> bool:
    flags = {"video": {"-c:v", "-codec:v"}, "audio": {"-c:a", "-codec:a"}}[media_type]
    return any(arg in flags and i + 1 < len(args) and args[i + 1] == "copy" for i, arg in enumerate(args))


def _replace_copy_codec(args: list[str], media_type: str, codec: str, extras: list[str]) -> list[str]:
    flags = {"video": {"-c:v", "-codec:v"}, "audio": {"-c:a", "-codec:a"}}[media_type]
    out: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in flags and i + 1 < len(args) and args[i + 1] == "copy":
            out.extend([args[i], codec, *extras])
            i += 2
        else:
            out.append(args[i])
            i += 1
    return out


def _insert_before_first_input(args: list[str], extra: list[str]) -> list[str]:
    if not extra or "-hwaccel" in args:
        return args
    try:
        idx = args.index("-i")
    except ValueError:
        return args
    return [*args[:idx], *extra, *args[idx:]]


def _encoder_for_mode(settings: dict[str, object], encoders: set[str], device_exists=os.path.exists) -> tuple[str, str]:
    """Return (encoder, hw kind). hw kind is vaapi, nvenc, or cpu."""
    mode = str(settings["transcoding_mode"]).lower()
    hw = str(settings["transcoding_hwaccel"]).lower()
    preferred = str(settings["transcoding_video_codec"]).lower()
    fallback = str(settings["transcoding_fallback_codec"]).lower() or "libx264"
    vaapi_device = str(settings["transcoding_vaapi_device"])

    if hw == "auto":
        if device_exists(vaapi_device):
            hw = "vaapi"
        elif any(x in encoders for x in ("h264_nvenc", "hevc_nvenc")):
            hw = "nvenc"
        else:
            hw = "cpu"

    if mode == "software" or hw == "cpu":
        target = "libx265" if mode == "hevc" else fallback
        return (target if not encoders or target in encoders else "libx264", "cpu")

    if mode == "hevc":
        target = "hevc_vaapi" if hw == "vaapi" else "hevc_nvenc" if hw == "nvenc" else "libx265"
    elif mode == "h264":
        target = "h264_vaapi" if hw == "vaapi" else "h264_nvenc" if hw == "nvenc" else fallback
    else:
        target = preferred
        if hw == "nvenc" and preferred.endswith("_vaapi"):
            target = "h264_nvenc" if preferred.startswith("h264") else "hevc_nvenc"
        elif hw == "vaapi" and preferred.endswith("_nvenc"):
            target = "h264_vaapi" if preferred.startswith("h264") else "hevc_vaapi"

    if target.endswith("_vaapi") and not device_exists(vaapi_device):
        return (fallback if not encoders or fallback in encoders else "libx264", "cpu")
    if encoders and target not in encoders:
        return (fallback if fallback in encoders else "libx264", "cpu")
    return target, "vaapi" if target.endswith("_vaapi") else "nvenc" if target.endswith("_nvenc") else "cpu"


def _video_extras(encoder: str, quality: int) -> list[str]:
    if encoder.endswith("_vaapi"):
        # Normalise 10-bit/other VAAPI surfaces to NV12 for broad H.264 compatibility.
        return ["-vf", "scale_vaapi=format=nv12", "-qp", str(quality)]
    if encoder.endswith("_nvenc"):
        return ["-preset", "p4", "-cq", str(quality)]
    if encoder in {"libx264", "libx265"}:
        return ["-preset", "veryfast", "-crf", str(quality)]
    return []


def transform_args(
    args: list[str],
    settings: dict[str, object],
    media: dict[str, str | None] | None,
    encoders: set[str] | None = None,
    device_exists=os.path.exists,
) -> tuple[list[str], str]:
    """Apply policy to copy decisions and return (args, human-readable decision)."""
    mode = str(settings.get("transcoding_mode", "auto")).lower()
    if mode in {"off", "copy", "passthrough", "disabled"}:
        return args, "passthrough"
    if mode not in {"auto", "h264", "hevc", "software"}:
        return args, f"unknown-mode:{mode}; passthrough"
    if media is None:
        return args, "probe-failed; passthrough"

    result = list(args)
    decisions: list[str] = []
    encoders = encoders if encoders is not None else available_encoders()
    direct_video = _csv(settings.get("transcoding_direct_video_codecs"))
    direct_audio = _csv(settings.get("transcoding_direct_audio_codecs"))
    video_codec = (media.get("video") or "").lower()
    audio_codec = (media.get("audio") or "").lower()

    force_video = mode in {"h264", "hevc", "software"}
    video_copy_allowed = _bool(settings.get("transcoding_copy_video"), True)
    if _has_copy(result, "video") and video_codec:
        if not force_video and video_copy_allowed and video_codec in direct_video:
            decisions.append(f"video={video_codec}->copy")
        else:
            encoder, hw = _encoder_for_mode(settings, encoders, device_exists=device_exists)
            quality = int(settings.get("transcoding_video_quality", 22))
            result = _replace_copy_codec(result, "video", encoder, _video_extras(encoder, quality))
            if _bool(settings.get("transcoding_hw_decode"), True):
                if hw == "vaapi":
                    result = _insert_before_first_input(
                        result,
                        ["-hwaccel", "vaapi", "-hwaccel_device", str(settings["transcoding_vaapi_device"]),
                         "-hwaccel_output_format", "vaapi"],
                    )
                elif hw == "nvenc":
                    result = _insert_before_first_input(result, ["-hwaccel", "cuda"])
            decisions.append(f"video={video_codec}->{encoder}")

    audio_copy_allowed = _bool(settings.get("transcoding_copy_audio"), True)
    if _has_copy(result, "audio") and audio_codec:
        if audio_copy_allowed and audio_codec in direct_audio:
            decisions.append(f"audio={audio_codec}->copy")
        else:
            audio_encoder = str(settings.get("transcoding_audio_codec", "aac")) or "aac"
            bitrate = str(settings.get("transcoding_audio_bitrate", "192k")) or "192k"
            extras = ["-b:a", bitrate]
            if audio_encoder == "aac":
                extras += ["-ac", "2"]
            result = _replace_copy_codec(result, "audio", audio_encoder, extras)
            decisions.append(f"audio={audio_codec}->{audio_encoder}")

    return result, ", ".join(decisions) if decisions else "upstream-transcode/passthrough"


def main() -> int:
    args = sys.argv[1:]
    if not os.path.exists(REAL_FFMPEG):
        print(f"[ffmpeg-policy] real ffmpeg not found: {REAL_FFMPEG}", file=sys.stderr)
        return 127

    settings = load_settings()
    media_url = _input_from_args(args)
    # Introspection commands (ffmpeg -version/-encoders/etc.) have no input and
    # must remain completely transparent.
    if not media_url:
        os.execv(REAL_FFMPEG, [REAL_FFMPEG, *args])

    needs_probe = _has_copy(args, "video") or _has_copy(args, "audio")
    media = probe_media(media_url) if needs_probe else {"video": None, "audio": None}
    transformed, decision = transform_args(args, settings, media)
    if needs_probe:
        print(f"[ffmpeg-policy] mode={settings['transcoding_mode']} {decision}", file=sys.stderr)
    os.execv(REAL_FFMPEG, [REAL_FFMPEG, *transformed])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
