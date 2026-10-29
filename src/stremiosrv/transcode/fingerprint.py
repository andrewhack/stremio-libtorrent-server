"""Decide, per stream, whether to copy or transcode — from the client's declared codec support
plus the probe result. Pure function (no ffmpeg), so it's unit-testable anywhere."""
from __future__ import annotations

# Each audio track of a multi-track transcode is a live AAC encode of its own, so a file carrying a
# dozen dubs does not get a dozen encoders.
MAX_AUDIO_TRACKS = 8


def decide(
    probe: dict,
    video_codecs: list[str],
    audio_codecs: list[str],
    max_audio_channels: int,
    max_width: int,
) -> dict:
    streams = probe.get("streams", [])
    v = next((s for s in streams if s.get("track") == "video"), None)
    audios = [s for s in streams if s.get("track") == "audio"]
    a = audios[0] if audios else None
    out: dict = {}
    if v is not None:
        over_width = v.get("width", 0) > max_width
        unsupported = v.get("codec") not in video_codecs
        if unsupported or over_width:
            out["video"] = {
                "action": "transcode",
                "scale_width": min(v.get("width", max_width), max_width),
            }
            if v.get("isHdr"):
                # The output is 8-bit SDR: without tone mapping, PQ/HLG-coded light comes out grey
                # and flat. A copied stream is left as HDR for a player that takes it.
                out["video"]["tonemap"] = True
        else:
            out["video"] = {"action": "copy"}
    if v is not None and len(audios) > 1:
        # Every track, each its own rendition the player can switch to (converter.build_hls_cmd).
        # All of them AAC stereo, which every player takes: one setting for every track, so none
        # fails to play because its codec was copied.
        out["audio"] = {"action": "transcode", "tracks": [
            {"index": s.get("index"), "lang": s.get("lang"), "title": s.get("title"),
             "default": bool(s.get("default"))}
            for s in audios[:MAX_AUDIO_TRACKS]
        ]}
    elif a is not None:
        bad_codec = a.get("codec") not in audio_codecs
        too_many = a.get("channels", 2) > max_audio_channels
        out["audio"] = {"action": "transcode" if (bad_codec or too_many) else "copy"}
    return out
