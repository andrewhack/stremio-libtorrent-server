"""Runtime fixes for the fork-owned transcoding policy.

This layer corrects telemetry against both pre-policy and policy-enabled server
images, reports when the running streaming-server image still lacks the FFmpeg
wrapper, and keeps the fix isolated from upstream/core source.
"""
from __future__ import annotations

import os
import re
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, HTMLResponse

import transcoding_config as base

app = base.app
STATIC = Path(__file__).with_name("static")
REAL_FFMPEG = "/usr/local/libexec/stremio/ffmpeg-real"
WRAPPER_FFMPEG = "/usr/local/bin/ffmpeg"
TARGET_SERVER_VERSION = "1.6.9-server.2"
_CAP_CACHE: dict[str, Any] = {"at": 0.0, "value": None, "binary": None}


def _exec(container, argv: list[str]):
    try:
        return container.exec_run(argv)
    except Exception:
        return None


def _exists(container, path: str, executable: bool = False) -> bool:
    flag = "-x" if executable else "-e"
    result = _exec(container, ["test", flag, path])
    return bool(result is not None and result.exit_code == 0)


def _read_server_version(container) -> str | None:
    result = _exec(container, ["cat", "/srv/app/SERVER_VERSION"])
    if result is None or result.exit_code != 0:
        return None
    value = result.output.decode("utf-8", errors="replace").strip()
    return value or None


def _wrapper_installed(container) -> bool:
    if not _exists(container, WRAPPER_FFMPEG, executable=True):
        return False
    result = _exec(
        container,
        ["sh", "-lc", f"grep -q 'ffmpeg-policy' {shlex.quote(WRAPPER_FFMPEG)}"],
    )
    return bool(result is not None and result.exit_code == 0)


def _ffmpeg_binary(container) -> str | None:
    if _exists(container, REAL_FFMPEG, executable=True):
        return REAL_FFMPEG
    result = _exec(container, ["sh", "-lc", "command -v ffmpeg || true"])
    if result is None:
        return None
    value = result.output.decode("utf-8", errors="replace").strip().splitlines()
    return value[0] if value else None


def _runtime_state(container) -> dict[str, object]:
    wrapper = _wrapper_installed(container)
    real = _exists(container, REAL_FFMPEG, executable=True)
    version = _read_server_version(container)
    binary = _ffmpeg_binary(container)
    ready = wrapper and real
    if ready:
        message = "FFmpeg policy wrapper is active in the streaming-server runtime."
    elif not wrapper:
        message = (
            "FFmpeg policy wrapper is not active in the running server image. "
            f"Update or rebuild the streaming server to {TARGET_SERVER_VERSION}."
        )
    else:
        message = "Policy wrapper exists, but the preserved real FFmpeg binary is missing. Rebuild the server image."
    return {
        "ready": ready,
        "wrapperInstalled": wrapper,
        "realFfmpegPresent": real,
        "ffmpegBinary": binary,
        "serverVersion": version,
        "targetServerVersion": TARGET_SERVER_VERSION,
        "message": message,
    }


def _normalise_process_command(command: str) -> str | None:
    """Return the FFmpeg argv portion from Docker top output.

    Old server images expose argv[0] as ``ffmpeg``; policy-enabled images exec
    ``ffmpeg-real``. Both are real active sessions and must be counted.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for index, token in enumerate(tokens):
        if os.path.basename(token) in {"ffmpeg", "ffmpeg-real"}:
            return shlex.join(tokens[index:])
    return None


def _process_commands(container) -> list[str]:
    try:
        data = container.top(ps_args="-eo args")
        rows = data.get("Processes", []) if isinstance(data, dict) else []
        raw = [" ".join(str(x) for x in row) for row in rows]
    except Exception:
        try:
            data = container.top()
            rows = data.get("Processes", []) if isinstance(data, dict) else []
            raw = [" ".join(str(x) for x in row) for row in rows]
        except Exception:
            return []
    commands = [_normalise_process_command(command) for command in raw]
    return [command for command in commands if command]


def _engine(video_target: str | None, audio_target: str | None = None) -> str:
    video = (video_target or "").lower()
    audio = (audio_target or "").lower()
    if video.endswith("_vaapi"):
        return "vaapi"
    if video.endswith("_nvenc"):
        return "nvenc"
    if video and video != "copy":
        return "cpu"
    if audio and audio != "copy":
        return "cpu-audio"
    if video == "copy" or audio == "copy":
        return "copy"
    return "none"


def _source_codecs_from_log(text: str) -> tuple[str | None, str | None]:
    video = None
    audio = None
    for line in text.replace("\r", "\n").splitlines():
        if video is None and "Video:" in line and "Stream #" in line:
            match = re.search(r"Video:\s*([A-Za-z0-9_.-]+)", line)
            if match:
                video = match.group(1).lower()
        if audio is None and "Audio:" in line and "Stream #" in line:
            match = re.search(r"Audio:\s*([A-Za-z0-9_.-]+)", line)
            if match:
                audio = match.group(1).lower()
        if video is not None and audio is not None:
            break
    return video, audio


def _session_from_command(container, cache_root: str, command: str) -> dict[str, object]:
    tokens = base._tokens(command)
    video_target = base._option(tokens, "-c:v", "-codec:v")
    audio_target = base._option(tokens, "-c:a", "-codec:a")
    job_id = base._job_id(command)
    log_text = base._read_job_log(container, cache_root, job_id)
    decision = base._parse_policy_log(log_text)
    progress = base._parse_progress(log_text)
    log_video, log_audio = _source_codecs_from_log(log_text)
    source_video = decision.get("sourceVideo") if decision else log_video
    source_audio = decision.get("sourceAudio") if decision else log_audio
    return {
        "jobId": job_id,
        "action": base._action(video_target, audio_target),
        "engine": _engine(video_target, audio_target),
        "sourceVideo": source_video,
        "targetVideo": video_target,
        "sourceAudio": source_audio,
        "targetAudio": audio_target,
        "policyDecision": decision.get("decision") if decision else None,
        "progress": progress,
    }


def _capabilities(container, policy: dict[str, object]) -> dict[str, object]:
    now = time.monotonic()
    binary = _ffmpeg_binary(container)
    cache_valid = (
        _CAP_CACHE["value"] is not None
        and _CAP_CACHE["binary"] == binary
        and now - float(_CAP_CACHE["at"]) < 60
    )
    if cache_valid:
        cached = dict(_CAP_CACHE["value"])
    else:
        encoders: set[str] = set()
        if binary:
            result = _exec(container, [binary, "-hide_banner", "-encoders"])
            if result is not None and result.exit_code == 0:
                text = result.output.decode("utf-8", errors="replace")
                # FFmpeg encoder flags are six characters and may include
                # letters such as D/F/S/X/B. The previous VAS-dot-only regex
                # rejected valid encoders such as libx264 and VAAPI/NVENC.
                encoders = set(
                    re.findall(r"^\s*[VAS][^\s]{5}\s+([^\s]+)", text, re.MULTILINE)
                )
        cached = {
            "h264Vaapi": "h264_vaapi" in encoders,
            "hevcVaapi": "hevc_vaapi" in encoders,
            "h264Nvenc": "h264_nvenc" in encoders,
            "hevcNvenc": "hevc_nvenc" in encoders,
            "libx264": "libx264" in encoders,
            "libx265": "libx265" in encoders,
            "detectedEncoderCount": len(encoders),
            "probedFfmpegBinary": binary,
        }
        _CAP_CACHE["at"] = now
        _CAP_CACHE["value"] = dict(cached)
        _CAP_CACHE["binary"] = binary

    device = str(policy.get("transcoding_vaapi_device") or "/dev/dri/renderD128")
    cached["vaapiDevice"] = device
    cached["vaapiDevicePresent"] = _exists(container, device)
    result = _exec(container, ["sh", "-lc", "ls -1 /dev/dri 2>/dev/null || true"])
    cached["driNodes"] = (
        result.output.decode("utf-8", errors="replace").splitlines()
        if result is not None and result.output
        else []
    )
    return cached


def _forced_target(mode: str, hw: str, preferred: str, fallback: str) -> str:
    if mode == "software":
        return fallback
    if mode == "h264":
        if hw == "vaapi":
            return "h264_vaapi"
        if hw == "nvenc":
            return "h264_nvenc"
        if hw == "cpu":
            return fallback
        return "auto-selected H.264 encoder"
    if mode == "hevc":
        if hw == "vaapi":
            return "hevc_vaapi"
        if hw == "nvenc":
            return "hevc_nvenc"
        if hw == "cpu":
            return "libx265"
        return "auto-selected HEVC encoder"
    return preferred


def _policy_summary(policy: dict[str, object]) -> str:
    mode = str(policy.get("transcoding_mode", "auto")).lower()
    hw = str(policy.get("transcoding_hwaccel", "auto")).lower()
    preferred = str(policy.get("transcoding_video_codec", "h264_vaapi"))
    audio = str(policy.get("transcoding_audio_codec", "aac"))
    bitrate = str(policy.get("transcoding_audio_bitrate", "192k"))
    fallback = str(policy.get("transcoding_fallback_codec", "libx264"))
    direct_video = str(policy.get("transcoding_direct_video_codecs", "h264"))
    direct_audio = str(policy.get("transcoding_direct_audio_codecs", "aac,mp3,ac3"))

    if mode in {"copy", "off", "passthrough", "disabled"}:
        return "COPY: policy disabled; upstream FFmpeg codec decisions pass through unchanged."
    if mode == "auto":
        return (
            f"AUTO: video copy stays direct for [{direct_video}]; other video copy decisions use "
            f"{preferred} via {hw}. Audio copy stays direct for [{direct_audio}]; other audio copy "
            f"decisions use {audio} {bitrate}. Existing upstream transcodes are preserved. "
            f"Software fallback: {fallback}."
        )
    target = _forced_target(mode, hw, preferred, fallback)
    return (
        f"{mode.upper()}: every video stream the core marked for copy is forced to {target}; "
        "video already selected for upstream transcoding is left unchanged. "
        f"Audio policy only changes audio copy decisions to {audio} {bitrate} when required. "
        f"Software fallback: {fallback}."
    )


# Patch the implementation used by base.transcoding_status without touching the
# original upstream-derived WebAdmin or stremiosrv source files.
base._process_commands = _process_commands
base._engine = _engine
base._session_from_command = _session_from_command
base._capabilities = _capabilities
base._policy_summary = _policy_summary
base._CAP_CACHE = {"at": 0.0, "value": None}
_original_status = base.transcoding_status


def transcoding_status():
    data = _original_status()
    if not isinstance(data, dict):
        return data
    try:
        container = base.legacy.client().containers.get(base.legacy.CONTAINER)
        runtime = _runtime_state(container)
        data["runtime"] = runtime
        warnings: list[str] = []
        if not runtime["ready"]:
            warnings.append(str(runtime["message"]))
        hardware = data.get("hardware") if isinstance(data.get("hardware"), dict) else {}
        policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
        if str(policy.get("transcoding_hwaccel", "")).lower() == "vaapi" and not hardware.get(
            "vaapiDevicePresent"
        ):
            warnings.append(
                f"VAAPI is selected but {hardware.get('vaapiDevice') or '/dev/dri/renderD128'} "
                "is not mounted in the server container. Start the server with compose.vaapi.yaml "
                "or the GPU overlay that exposes /dev/dri."
            )
        data["warnings"] = warnings
    except Exception as exc:
        data["runtime"] = {"ready": False, "message": str(exc)}
        data["warnings"] = [str(exc)]
    return data


RUNTIME_SCRIPT_TAG = '<script src="/transcoding-runtime-fix.js"></script>'


def runtime_home():
    response = base.transcoding_home()
    text = response.body.decode("utf-8", errors="replace")
    if RUNTIME_SCRIPT_TAG not in text:
        text = text.replace("</body>", f"  {RUNTIME_SCRIPT_TAG}\n</body>")
    return HTMLResponse(text, headers={"Cache-Control": "no-store"})


def runtime_script():
    return FileResponse(
        STATIC / "transcoding-runtime-fix.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


def _replace_route(path: str) -> None:
    app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) != path]


_replace_route("/api/transcoding/status")
app.add_api_route("/api/transcoding/status", transcoding_status, methods=["GET"])
_replace_route("/")
app.add_api_route("/", runtime_home, methods=["GET"])
app.add_api_route("/transcoding-runtime-fix.js", runtime_script, methods=["GET"])
