#!/usr/bin/env python3
"""Fork-owned FFmpeg execution-profile wrapper.

The Stremio core still decides whether a stream needs transcoding. This wrapper
only replaces the *video encoder* when the operator selected one explicit
execution profile. Copy remains copy; no hidden codec choice and no silent
software fallback are introduced by the wrapper.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REAL_FFMPEG = os.getenv("FFMPEG_REAL", "/usr/local/libexec/stremio/ffmpeg-real")
CONFIG_FILE = os.getenv("STREMIOSRV_EXTERNAL_CONFIG", "/config/admin-settings.json")
VAAPI_DEVICE_DEFAULT = "/dev/dri/renderD128"

PROFILES = {
    "preserve": {"encoder": None, "engine": "core", "label": "Preserve Stremio decision"},
    "vaapi-h264": {"encoder": "h264_vaapi", "engine": "vaapi", "label": "H.264 VAAPI"},
    "vaapi-hevc": {"encoder": "hevc_vaapi", "engine": "vaapi", "label": "HEVC VAAPI"},
    "nvenc-h264": {"encoder": "h264_nvenc", "engine": "nvenc", "label": "H.264 NVENC"},
    "nvenc-hevc": {"encoder": "hevc_nvenc", "engine": "nvenc", "label": "HEVC NVENC"},
    "cpu-h264": {"encoder": "libx264", "engine": "cpu", "label": "H.264 CPU"},
    "cpu-hevc": {"encoder": "libx265", "engine": "cpu", "label": "HEVC CPU"},
}


def _read_config() -> dict[str, object]:
    try:
        data = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _profile(config: dict[str, object]) -> str | None:
    value = str(config.get("transcoding_profile") or "").strip().lower()
    return value if value in PROFILES else None


def _vaapi_device(config: dict[str, object]) -> str:
    return str(config.get("transcoding_vaapi_device") or os.getenv("VAAPI_DEVICE") or VAAPI_DEVICE_DEFAULT)


def _quality(config: dict[str, object]) -> int:
    try:
        return max(0, min(51, int(config.get("transcoding_video_quality", 22))))
    except (TypeError, ValueError):
        return 22


def _available_encoders() -> set[str]:
    try:
        result = subprocess.run(
            [REAL_FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(re.findall(r"^\s*[VAS][^\s]{5}\s+([^\s]+)", result.stdout, re.MULTILINE))


def _video_codec(args: list[str]) -> tuple[int | None, str | None]:
    for index, token in enumerate(args[:-1]):
        if token in {"-c:v", "-codec:v"}:
            return index, args[index + 1]
    return None, None


def _remove_option(args: list[str], names: set[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(args):
        if args[index] in names and index + 1 < len(args):
            index += 2
            continue
        out.append(args[index])
        index += 1
    return out


def _replace_option(args: list[str], names: set[str], value: str) -> list[str]:
    out = list(args)
    for index, token in enumerate(out[:-1]):
        if token in names:
            out[index + 1] = value
            return out
    return out


def _insert_before_input(args: list[str], extra: list[str]) -> list[str]:
    if not extra:
        return args
    try:
        index = args.index("-i")
    except ValueError:
        return args
    return [*args[:index], *extra, *args[index:]]


def _extract_scale(filter_value: str | None) -> str | None:
    if not filter_value:
        return None
    match = re.search(r"(?:scale|scale_vaapi)(?:=w=|=)(\d+)", filter_value)
    return match.group(1) if match else None


def _existing_filter(args: list[str]) -> str | None:
    for index, token in enumerate(args[:-1]):
        if token in {"-vf", "-filter:v"}:
            return args[index + 1]
    return None


def _normalise_filter_for_software_frames(args: list[str]) -> tuple[list[str], str | None]:
    """Remove old hardware filters and preserve only an explicit scale width.

    Profiles intentionally assume nothing about decoder support. VAAPI/NVENC
    profiles therefore use normal software-decoded frames and hardware encode.
    """
    old_filter = _existing_filter(args)
    width = _extract_scale(old_filter)
    result = _remove_option(args, {"-vf", "-filter:v"})
    base = f"scale={width}:-2:flags=lanczos" if width else None
    return result, base


def _strip_encoder_tuning(args: list[str]) -> list[str]:
    return _remove_option(
        args,
        {
            "-preset", "-tune", "-crf", "-cq", "-qp", "-global_quality",
            "-rc", "-profile:v", "-level:v",
        },
    )


def _strip_hw_decode(args: list[str]) -> list[str]:
    return _remove_option(
        args,
        {"-hwaccel", "-hwaccel_device", "-hwaccel_output_format", "-vaapi_device"},
    )


def _apply_profile(args: list[str], profile_name: str, config: dict[str, object]) -> tuple[list[str], str]:
    profile = PROFILES[profile_name]
    target = profile["encoder"]
    if target is None:
        return args, "profile=preserve; core decision unchanged"

    codec_index, current = _video_codec(args)
    if codec_index is None or current is None:
        return args, f"profile={profile_name}; no video codec option found"

    # Stremio's Direct Stream decision remains authoritative. Profiles only
    # replace an actual transcode encoder, preventing unnecessary re-encoding.
    if current == "copy":
        return args, f"profile={profile_name}; video=copy preserved"

    encoders = _available_encoders()
    if target not in encoders:
        return args, f"profile={profile_name}; unavailable encoder {target}; core encoder {current} preserved"

    device = _vaapi_device(config)
    if profile["engine"] == "vaapi" and not os.path.exists(device):
        return args, f"profile={profile_name}; VAAPI device {device} unavailable; core encoder {current} preserved"

    quality = _quality(config)
    result = _strip_encoder_tuning(_strip_hw_decode(args))
    result, software_filter = _normalise_filter_for_software_frames(result)
    result = _replace_option(result, {"-c:v", "-codec:v"}, str(target))

    if profile["engine"] == "vaapi":
        # Decode stays software by design. This avoids pretending every source
        # codec has a working VAAPI decoder while still moving the expensive
        # encode stage to the GPU.
        result = _insert_before_input(result, ["-vaapi_device", device])
        vf = f"{software_filter},format=nv12,hwupload" if software_filter else "format=nv12,hwupload"
        result = _insert_before_output_codec_options(result, ["-vf", vf, "-qp", str(quality)])
    elif profile["engine"] == "nvenc":
        if software_filter:
            result = _insert_before_output_codec_options(result, ["-vf", software_filter])
        result = _insert_before_output_codec_options(result, ["-preset", "p4", "-cq", str(quality)])
    else:
        if software_filter:
            result = _insert_before_output_codec_options(result, ["-vf", software_filter])
        result = _insert_before_output_codec_options(result, ["-preset", "veryfast", "-crf", str(quality)])

    return result, f"profile={profile_name}; video={current}->{target}; decode=software"


def _insert_before_output_codec_options(args: list[str], extra: list[str]) -> list[str]:
    """Insert tuning/filter options immediately before -c:v."""
    for index, token in enumerate(args):
        if token in {"-c:v", "-codec:v"}:
            return [*args[:index], *extra, *args[index:]]
    return args


def _legacy_passthrough(args: list[str], config: dict[str, object]) -> tuple[list[str], str]:
    """No new guess for installations that have not selected a profile yet."""
    legacy_mode = str(config.get("transcoding_mode") or "").strip().lower()
    if legacy_mode:
        return args, f"legacy settings detected ({legacy_mode}); select an explicit profile in WebAdmin"
    return args, "no explicit profile; core decision unchanged"


def main() -> int:
    args = sys.argv[1:]
    if not os.path.exists(REAL_FFMPEG):
        print(f"[ffmpeg-policy] real ffmpeg not found: {REAL_FFMPEG}", file=sys.stderr)
        return 127

    if "-i" not in args:
        os.execv(REAL_FFMPEG, [REAL_FFMPEG, *args])

    config = _read_config()
    selected = _profile(config)
    if selected is None:
        transformed, decision = _legacy_passthrough(args, config)
    else:
        transformed, decision = _apply_profile(args, selected, config)

    print(f"[ffmpeg-policy] {decision}", file=sys.stderr)
    os.execv(REAL_FFMPEG, [REAL_FFMPEG, *transformed])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
