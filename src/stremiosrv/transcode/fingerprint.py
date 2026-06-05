"""Decide, per stream, whether to copy or transcode — from the client's declared codec support
plus the probe result. Pure function (no ffmpeg), so it's unit-testable anywhere."""
from __future__ import annotations


def decide(
    probe: dict,
    video_codecs: list[str],
    audio_codecs: list[str],
    max_audio_channels: int,
    max_width: int,
) -> dict:
    streams = probe.get("streams", [])
    v = next((s for s in streams if s.get("track") == "video"), None)
    a = next((s for s in streams if s.get("track") == "audio"), None)
    out: dict = {}
    if v is not None:
        over_width = v.get("width", 0) > max_width
        unsupported = v.get("codec") not in video_codecs
        if unsupported or over_width:
            out["video"] = {
                "action": "transcode",
                "scale_width": min(v.get("width", max_width), max_width),
            }
        else:
            out["video"] = {"action": "copy"}
    if a is not None:
        bad_codec = a.get("codec") not in audio_codecs
        too_many = a.get("channels", 2) > max_audio_channels
        out["audio"] = {"action": "transcode" if (bad_codec or too_many) else "copy"}
    return out
