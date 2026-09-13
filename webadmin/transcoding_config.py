"""WebAdmin extension for the fork-owned FFmpeg transcoding policy.

The extension keeps transcoding policy and telemetry outside the upstream
streaming-server source. It extends the generic configuration API, exposes
read-only runtime telemetry collected through Docker, and injects a dedicated
Dashboard UI without modifying the base WebAdmin HTML.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, HTMLResponse

import app as legacy

TRANSCODING_DESCRIPTIONS = {
    "transcoding_mode": (
        "FFmpeg policy: auto keeps Direct Stream when compatible; copy disables policy changes; "
        "h264/hevc/software force video conversion only where the core requested stream copy."
    ),
    "transcoding_hwaccel": "Preferred acceleration: auto, vaapi, nvenc or cpu.",
    "transcoding_vaapi_device": "VAAPI render node used by the FFmpeg policy wrapper.",
    "transcoding_video_codec": "Preferred video encoder in auto mode, for example h264_vaapi.",
    "transcoding_video_quality": "Video quality value from 0 to 51; 22 is the recommended default.",
    "transcoding_audio_codec": "Codec used when audio conversion is required; AAC is the default.",
    "transcoding_audio_bitrate": "Bitrate used for converted audio, for example 192k.",
    "transcoding_copy_video": "Keep video as Direct Stream when its codec is in the compatible list.",
    "transcoding_copy_audio": "Keep audio as Direct Stream when its codec is in the compatible list.",
    "transcoding_direct_video_codecs": "Video codecs allowed for Direct Stream, comma-separated.",
    "transcoding_direct_audio_codecs": "Audio codecs allowed for Direct Stream, comma-separated.",
    "transcoding_fallback_codec": "Software encoder used when the selected hardware encoder is unavailable.",
    "transcoding_hw_decode": "Use hardware decoding together with a hardware encoder when possible.",
}

TRANSCODING_DEFAULTS = {
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

legacy.DESCRIPTIONS.update(TRANSCODING_DESCRIPTIONS)
legacy.DEFAULTS.update(TRANSCODING_DEFAULTS)

# Import only after patching the shared legacy module. version_lifecycle ->
# fork_update -> app reuses this same module object from sys.modules.
import version_lifecycle as lifecycle  # noqa: E402

app = lifecycle.app
STATIC = Path(__file__).with_name("static")
REAL_FFMPEG = "/usr/local/libexec/stremio/ffmpeg-real"
_CAP_CACHE: dict[str, Any] = {"at": 0.0, "value": None}


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normalise(name: str, value: object) -> object:
    if name in {"transcoding_copy_video", "transcoding_copy_audio", "transcoding_hw_decode"}:
        return _bool(value, bool(TRANSCODING_DEFAULTS[name]))
    if name == "transcoding_video_quality":
        try:
            return max(0, min(51, int(value)))
        except (TypeError, ValueError):
            return TRANSCODING_DEFAULTS[name]
    return str(value).strip() if value is not None else TRANSCODING_DEFAULTS[name]


def _container_env(container) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        container.reload()
        for item in container.attrs.get("Config", {}).get("Env", []) or []:
            if "=" in item:
                key, value = item.split("=", 1)
                result[key] = value
    except Exception:
        pass
    return result


def _effective_policy(container) -> dict[str, object]:
    """Mirror wrapper precedence: built-ins -> container env -> WebAdmin JSON."""
    values = dict(TRANSCODING_DEFAULTS)
    env = _container_env(container)
    for name, env_name in ENV_MAP.items():
        if env_name in env:
            values[name] = _normalise(name, env[env_name])
    config = legacy.read_config()
    for name in TRANSCODING_DEFAULTS:
        if name in config:
            values[name] = _normalise(name, config[name])
    return values


def _cache_root(container) -> str:
    return _container_env(container).get("STREMIOSRV_CACHE_ROOT", "/root/.stremio-server").rstrip("/")


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _option(tokens: list[str], *names: str) -> str | None:
    for index, token in enumerate(tokens[:-1]):
        if token in names:
            return tokens[index + 1]
    return None


def _engine(video_target: str | None) -> str:
    target = (video_target or "").lower()
    if target == "copy":
        return "copy"
    if target.endswith("_vaapi"):
        return "vaapi"
    if target.endswith("_nvenc"):
        return "nvenc"
    if target:
        return "cpu"
    return "none"


def _action(video_target: str | None, audio_target: str | None) -> str:
    targets = [value for value in (video_target, audio_target) if value]
    if targets and all(value == "copy" for value in targets):
        return "direct-stream"
    if any(value != "copy" for value in targets):
        return "transcoding"
    return "unknown"


def _process_commands(container) -> list[str]:
    """Return active ffmpeg-real command lines without depending on host PID layout."""
    try:
        data = container.top(ps_args="-eo args")
        rows = data.get("Processes", []) if isinstance(data, dict) else []
        commands = [" ".join(str(x) for x in row) for row in rows]
    except Exception:
        try:
            data = container.top()
            rows = data.get("Processes", []) if isinstance(data, dict) else []
            commands = [" ".join(str(x) for x in row) for row in rows]
        except Exception:
            return []
    return [cmd for cmd in commands if "ffmpeg-real" in cmd]


def _job_id(command: str) -> str | None:
    match = re.search(r"/transcode/([A-Za-z0-9_.-]+)/", command)
    return match.group(1) if match else None


def _read_job_log(container, cache_root: str, job_id: str | None) -> str:
    if not job_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id):
        return ""
    path = f"{cache_root}/transcode/{job_id}/ffmpeg.log"
    try:
        result = container.exec_run(["tail", "-n", "80", path])
        if result.exit_code == 0:
            return result.output.decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _latest_job_log(container, cache_root: str) -> str:
    script = (
        'latest=$(ls -1t "$1"/transcode/*/ffmpeg.log 2>/dev/null | head -n 1); '
        '[ -n "$latest" ] && tail -n 80 "$latest" || true'
    )
    try:
        result = container.exec_run(["sh", "-c", script, "sh", cache_root])
        return result.output.decode("utf-8", errors="replace") if result.output else ""
    except Exception:
        return ""


def _parse_policy_log(text: str) -> dict[str, object] | None:
    policy_lines = [line for line in text.replace("\r", "\n").splitlines() if "[ffmpeg-policy]" in line]
    if not policy_lines:
        return None
    line = policy_lines[-1]
    match = re.search(r"\[ffmpeg-policy\]\s+mode=(\S+)\s+(.*)$", line)
    if not match:
        return {"raw": line}
    mode, decision = match.groups()
    result: dict[str, object] = {"mode": mode, "decision": decision}
    for kind in ("video", "audio"):
        item = re.search(rf"{kind}=([^,\s]+)->([^,\s]+)", decision)
        if item:
            result[f"source{kind.title()}"] = item.group(1)
            result[f"target{kind.title()}"] = item.group(2)
    return result


def _parse_progress(text: str) -> dict[str, object] | None:
    progress = None
    for line in text.replace("\r", "\n").splitlines():
        if "frame=" not in line and "size=" not in line:
            continue
        frame = re.search(r"frame=\s*(\d+)", line)
        fps = re.search(r"fps=\s*([0-9.]+)", line)
        bitrate = re.search(r"bitrate=\s*([^\s]+)", line)
        speed = re.search(r"speed=\s*([^\s]+)", line)
        if frame or fps or bitrate or speed:
            progress = {
                "frame": int(frame.group(1)) if frame else None,
                "fps": float(fps.group(1)) if fps else None,
                "bitrate": bitrate.group(1) if bitrate else None,
                "speed": speed.group(1) if speed else None,
            }
    return progress


def _session_from_command(container, cache_root: str, command: str) -> dict[str, object]:
    tokens = _tokens(command)
    video_target = _option(tokens, "-c:v", "-codec:v")
    audio_target = _option(tokens, "-c:a", "-codec:a")
    job_id = _job_id(command)
    log_text = _read_job_log(container, cache_root, job_id)
    decision = _parse_policy_log(log_text)
    progress = _parse_progress(log_text)
    source_video = decision.get("sourceVideo") if decision else None
    source_audio = decision.get("sourceAudio") if decision else None
    # The policy log is authoritative for wrapper decisions. The active command
    # is authoritative for the encoder actually running after fallback.
    return {
        "jobId": job_id,
        "action": _action(video_target, audio_target),
        "engine": _engine(video_target),
        "sourceVideo": source_video,
        "targetVideo": video_target,
        "sourceAudio": source_audio,
        "targetAudio": audio_target,
        "policyDecision": decision.get("decision") if decision else None,
        "progress": progress,
    }


def _capabilities(container, policy: dict[str, object]) -> dict[str, object]:
    now = time.monotonic()
    if _CAP_CACHE["value"] is not None and now - float(_CAP_CACHE["at"]) < 60:
        cached = dict(_CAP_CACHE["value"])
    else:
        encoders: set[str] = set()
        try:
            result = container.exec_run([REAL_FFMPEG, "-hide_banner", "-encoders"])
            if result.exit_code == 0:
                text = result.output.decode("utf-8", errors="replace")
                encoders = set(re.findall(r"^\s*[VAS\.]{6}\s+([\w-]+)", text, re.MULTILINE))
        except Exception:
            pass
        cached = {
            "h264Vaapi": "h264_vaapi" in encoders,
            "hevcVaapi": "hevc_vaapi" in encoders,
            "h264Nvenc": "h264_nvenc" in encoders,
            "hevcNvenc": "hevc_nvenc" in encoders,
            "libx264": "libx264" in encoders,
            "libx265": "libx265" in encoders,
        }
        _CAP_CACHE["at"] = now
        _CAP_CACHE["value"] = dict(cached)

    device = str(policy.get("transcoding_vaapi_device") or "/dev/dri/renderD128")
    try:
        device_result = container.exec_run(["test", "-e", device])
        vaapi_device_present = device_result.exit_code == 0
    except Exception:
        vaapi_device_present = False
    cached["vaapiDevice"] = device
    cached["vaapiDevicePresent"] = vaapi_device_present
    return cached


def _nvenc_utilisation(container) -> float | None:
    try:
        result = container.exec_run(
            ["nvidia-smi", "--query-gpu=utilization.encoder", "--format=csv,noheader,nounits"]
        )
        if result.exit_code != 0:
            return None
        values = [float(x.strip()) for x in result.output.decode().splitlines() if x.strip()]
        return round(max(values), 1) if values else None
    except Exception:
        return None


def _policy_summary(policy: dict[str, object]) -> str:
    mode = str(policy["transcoding_mode"])
    hw = str(policy["transcoding_hwaccel"])
    video = str(policy["transcoding_video_codec"])
    audio = str(policy["transcoding_audio_codec"])
    bitrate = str(policy["transcoding_audio_bitrate"])
    fallback = str(policy["transcoding_fallback_codec"])
    if mode in {"copy", "off", "passthrough", "disabled"}:
        return "Policy disabled: upstream Direct Stream/transcode decisions pass through unchanged."
    return (
        f"{mode.upper()}: compatible streams stay direct; incompatible video uses {video} via {hw}, "
        f"audio uses {audio} {bitrate}, fallback {fallback}."
    )


@app.get("/api/transcoding/status")
def transcoding_status():
    """Operational view of the wrapper policy and currently running FFmpeg jobs."""
    try:
        container = legacy.client().containers.get(legacy.CONTAINER)
        policy = _effective_policy(container)
        cache_root = _cache_root(container)
        sessions = [
            _session_from_command(container, cache_root, command)
            for command in _process_commands(container)
        ]
        latest_log = _latest_job_log(container, cache_root)
        latest_decision = _parse_policy_log(latest_log)
        latest_progress = _parse_progress(latest_log)
        engines = sorted({str(session["engine"]) for session in sessions if session["engine"] != "none"})
        nvenc_util = _nvenc_utilisation(container) if "nvenc" in engines else None
        return {
            "available": True,
            "checkedAt": datetime.now(UTC).isoformat(),
            "policy": policy,
            "policySummary": _policy_summary(policy),
            "hardware": {
                **_capabilities(container, policy),
                "encoderUtilizationPercent": nvenc_util,
                "encoderUtilizationSource": "nvidia-smi" if nvenc_util is not None else None,
            },
            "active": {
                "total": len(sessions),
                "transcoding": sum(s["action"] == "transcoding" for s in sessions),
                "directStream": sum(s["action"] == "direct-stream" for s in sessions),
                "engines": engines,
                "sessions": sessions,
            },
            "latestDecision": latest_decision,
            "latestProgress": latest_progress,
        }
    except Exception as exc:
        return {
            "available": False,
            "checkedAt": datetime.now(UTC).isoformat(),
            "message": str(exc),
            "policy": TRANSCODING_DEFAULTS | legacy.read_config(),
        }


TRANSCODING_SCRIPT_TAG = '<script src="/transcoding-dashboard.js"></script>'


def transcoding_home():
    """Build on the lifecycle page and inject only the fork-owned dashboard script."""
    response = lifecycle.lifecycle_home()
    text = response.body.decode("utf-8", errors="replace")
    if TRANSCODING_SCRIPT_TAG not in text:
        text = text.replace("</body>", f"  {TRANSCODING_SCRIPT_TAG}\n</body>")
    return HTMLResponse(text, headers={"Cache-Control": "no-store"})


def transcoding_script():
    return FileResponse(
        STATIC / "transcoding-dashboard.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


def _replace_route(path: str) -> None:
    app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) != path]


_replace_route("/")
app.add_api_route("/", transcoding_home, methods=["GET"])
app.add_api_route("/transcoding-dashboard.js", transcoding_script, methods=["GET"])
