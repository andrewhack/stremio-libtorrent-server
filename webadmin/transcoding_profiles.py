"""Simple, verified transcoding profiles for WebAdmin.

Profiles are offered only after a one-frame encoder self-test in the running
streaming-server container. This avoids presenting a compiled FFmpeg encoder as
usable hardware when the device/driver cannot actually initialise it.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

import transcoding_runtime_fix as base

app = base.app
legacy = base.base.legacy
STATIC = Path(__file__).with_name("static")
PROFILE_CACHE: dict[str, object] = {"at": 0.0, "value": None}

PROFILE_META = {
    "preserve": {
        "label": "Preserve Stremio decision",
        "encoder": None,
        "engine": "core",
        "codec": "unchanged",
        "description": "No encoder override. Stremio keeps full control.",
    },
    "vaapi-h264": {
        "label": "H.264 VAAPI",
        "encoder": "h264_vaapi",
        "engine": "vaapi",
        "codec": "H.264",
        "description": "Keep Direct Stream; use Intel/VAAPI H.264 only when Stremio requests video transcoding.",
    },
    "vaapi-hevc": {
        "label": "HEVC VAAPI",
        "encoder": "hevc_vaapi",
        "engine": "vaapi",
        "codec": "HEVC",
        "description": "Keep Direct Stream; use Intel/VAAPI HEVC only when Stremio requests video transcoding.",
    },
    "nvenc-h264": {
        "label": "H.264 NVIDIA NVENC",
        "encoder": "h264_nvenc",
        "engine": "nvenc",
        "codec": "H.264",
        "description": "Keep Direct Stream; use NVIDIA NVENC H.264 only when Stremio requests video transcoding.",
    },
    "nvenc-hevc": {
        "label": "HEVC NVIDIA NVENC",
        "encoder": "hevc_nvenc",
        "engine": "nvenc",
        "codec": "HEVC",
        "description": "Keep Direct Stream; use NVIDIA NVENC HEVC only when Stremio requests video transcoding.",
    },
    "cpu-h264": {
        "label": "H.264 CPU (libx264)",
        "encoder": "libx264",
        "engine": "cpu",
        "codec": "H.264",
        "description": "Keep Direct Stream; use libx264 only when Stremio requests video transcoding.",
    },
    "cpu-hevc": {
        "label": "HEVC CPU (libx265)",
        "encoder": "libx265",
        "engine": "cpu",
        "codec": "HEVC",
        "description": "Keep Direct Stream; use libx265 only when Stremio requests video transcoding.",
    },
}


class ProfileBody(BaseModel):
    profile: str
    quality: int = Field(default=22, ge=0, le=51)


def _exec(container, argv: list[str]):
    try:
        return container.exec_run(argv)
    except Exception:
        return None


def _selected() -> tuple[str, int]:
    config = legacy.read_config()
    profile = str(config.get("transcoding_profile") or "").strip().lower()
    if profile not in PROFILE_META:
        profile = "legacy"
    try:
        quality = max(0, min(51, int(config.get("transcoding_video_quality", 22))))
    except (TypeError, ValueError):
        quality = 22
    return profile, quality


def _test_profile(container, profile_id: str, device: str, binary: str | None) -> dict[str, object]:
    meta = PROFILE_META[profile_id]
    if profile_id == "preserve":
        return {**meta, "id": profile_id, "available": True, "verified": True, "reason": "No encoder required."}
    if not binary:
        return {**meta, "id": profile_id, "available": False, "verified": False, "reason": "FFmpeg binary not available."}

    encoder = str(meta["encoder"])
    if meta["engine"] == "vaapi":
        if not base._exists(container, device):
            return {**meta, "id": profile_id, "available": False, "verified": True, "reason": f"{device} is not mounted."}
        argv = [
            binary, "-hide_banner", "-loglevel", "error",
            "-vaapi_device", device,
            "-f", "lavfi", "-i", "color=c=black:s=128x72:d=0.04",
            "-vf", "format=nv12,hwupload", "-frames:v", "1",
            "-c:v", encoder, "-f", "null", "-",
        ]
    else:
        argv = [
            binary, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=128x72:d=0.04",
            "-frames:v", "1", "-c:v", encoder, "-f", "null", "-",
        ]

    result = _exec(container, argv)
    ok = bool(result is not None and result.exit_code == 0)
    if ok:
        reason = "One-frame encoder self-test passed."
    else:
        raw = result.output.decode("utf-8", errors="replace").strip() if result is not None and result.output else "encoder self-test failed"
        reason = raw.splitlines()[-1][:180] if raw else "encoder self-test failed"
    return {**meta, "id": profile_id, "available": ok, "verified": True, "reason": reason}


def _profiles(force: bool = False) -> dict[str, object]:
    now = time.monotonic()
    if not force and PROFILE_CACHE["value"] is not None and now - float(PROFILE_CACHE["at"]) < 300:
        return dict(PROFILE_CACHE["value"])

    container = legacy.client().containers.get(legacy.CONTAINER)
    binary = base._ffmpeg_binary(container)
    config = legacy.read_config()
    device = str(config.get("transcoding_vaapi_device") or "/dev/dri/renderD128")
    items = [_test_profile(container, profile_id, device, binary) for profile_id in PROFILE_META]
    selected, quality = _selected()
    value = {
        "checkedAt": datetime.now(UTC).isoformat(),
        "selected": selected,
        "quality": quality,
        "device": device,
        "ffmpeg": binary,
        "profiles": items,
        "rule": "Direct Stream remains Direct Stream. The selected profile is used only when Stremio already requires video transcoding. No silent encoder fallback.",
    }
    PROFILE_CACHE["at"] = now
    PROFILE_CACHE["value"] = dict(value)
    return value


@app.get("/api/transcoding/profiles")
def profiles():
    return _profiles()


@app.post("/api/transcoding/profiles/refresh")
def refresh_profiles():
    return _profiles(force=True)


@app.put("/api/transcoding/profile")
def set_profile(body: ProfileBody):
    available = _profiles(force=True)
    candidates = {str(item["id"]): item for item in available["profiles"]}
    profile = body.profile.strip().lower()
    if profile not in candidates:
        raise HTTPException(400, "unknown transcoding profile")
    if not candidates[profile].get("available"):
        raise HTTPException(409, f"profile is not available: {candidates[profile].get('reason')}")
    legacy.write_config({
        "transcoding_profile": profile,
        "transcoding_video_quality": body.quality,
    })
    legacy.audit("transcoding.profile", f"profile={profile} quality={body.quality}")
    PROFILE_CACHE["at"] = 0.0
    PROFILE_CACHE["value"] = None
    return {"ok": True, "profile": profile, "quality": body.quality, "restartRequired": False}


_original_status = base.transcoding_status


def _profile_summary(profile: str, quality: int) -> str:
    if profile == "legacy":
        return "Legacy transcoding fields are present. Choose one explicit verified profile in All Configuration."
    meta = PROFILE_META[profile]
    if profile == "preserve":
        return "PRESERVE: Stremio controls copy and transcoding; this policy does not replace the video encoder."
    return (
        f"{meta['label']}: Direct Stream stays direct. When Stremio requires video transcoding, "
        f"the encoder is explicitly {meta['encoder']} at quality {quality}. Decoder remains software. "
        "No silent fallback to another encoder."
    )


def transcoding_status():
    data = _original_status()
    if not isinstance(data, dict):
        return data
    selected, quality = _selected()
    data["executionProfile"] = {"id": selected, "quality": quality}
    data["policySummary"] = _profile_summary(selected, quality)
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    if selected in PROFILE_META:
        meta = PROFILE_META[selected]
        policy["transcoding_mode"] = meta["codec"] if selected != "preserve" else "preserve"
        policy["transcoding_hwaccel"] = meta["engine"]
        policy["transcoding_video_codec"] = meta["encoder"] or "core"
        data["policy"] = policy
        if meta["engine"] in {"vaapi", "nvenc"}:
            active = data.get("active") if isinstance(data.get("active"), dict) else {}
            sessions = active.get("sessions") if isinstance(active.get("sessions"), list) else []
            mismatches = [s for s in sessions if s.get("action") == "transcoding" and s.get("engine") != meta["engine"]]
            if mismatches:
                warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
                warnings.append(
                    f"{meta['label']} is selected but an existing transcoding job is using another engine. "
                    "Stop/restart playback so the new FFmpeg job uses the selected profile."
                )
                data["warnings"] = warnings
    return data


SCRIPT_TAG = '<script src="/transcoding-simple-config.js"></script>'


def profile_home():
    response = base.runtime_home()
    text = response.body.decode("utf-8", errors="replace")
    if SCRIPT_TAG not in text:
        text = text.replace("</body>", f"  {SCRIPT_TAG}\n</body>")
    return HTMLResponse(text, headers={"Cache-Control": "no-store"})


def profile_script():
    return FileResponse(STATIC / "transcoding-simple-config.js", media_type="application/javascript", headers={"Cache-Control": "no-store"})


def _replace_route(path: str) -> None:
    app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) != path]


_replace_route("/api/transcoding/status")
app.add_api_route("/api/transcoding/status", transcoding_status, methods=["GET"])
_replace_route("/")
app.add_api_route("/", profile_home, methods=["GET"])
app.add_api_route("/transcoding-simple-config.js", profile_script, methods=["GET"])
