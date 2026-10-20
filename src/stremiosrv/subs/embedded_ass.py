"""Embedded ASS subtitles for TV players: the pure half, with no I/O.

stremio-video 0.0.97+ lets a TV draw a file's embedded ASS/SSA tracks with their own styles and
fonts. A TV never transcodes -- it plays the file directly -- so the player asks the streaming
server these things (stremio-video withStreamingServer.js and withHTMLSubtitles.js):

    GET /embedded-ass?mediaURL=<u>[&deferFonts=1]           which ASS tracks and fonts the file has
    GET /embedded-ass/fonts?mediaURL=<u>                    its fonts, if the answer deferred them
    GET /embedded-ass/<number>.ass?mediaURL=<u>&from=&to=   one track's events, a window at a time
    GET /embedded-ass/font/<id>?mediaURL=<u>                one font attachment's bytes

Stock answers them since server.js 4.22.0 (deferFonts and /fonts since 4.22.3), reading the
Matroska file itself; there a track is named by its Matroska TrackNumber. The routes are
api/embedded_ass.py; this module is what they compute.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# The client's own font test (withStreamingServer.js FONT_MIME_TYPES / FONT_EXTENSION_PATTERN):
# the attachments it would treat as fonts are exactly the ones offered.
FONT_MIME_TYPES = frozenset({
    "application/font-sfnt", "application/font-woff", "application/vnd.ms-fontobject",
    "application/x-font-opentype", "application/x-font-ttf", "application/x-truetype-font",
    "font/collection", "font/otf", "font/sfnt", "font/ttf", "font/woff", "font/woff2",
})
_FONT_EXT = re.compile(r"\.(?:otc|otf|ttc|ttf|woff2?)$", re.IGNORECASE)
ASS_CODECS = frozenset({"ass", "ssa"})
# Events that start up to this long before a window are included. ffmpeg drops every event that
# starts before its seek point, and a line still on screen when a window begins belongs in it.
LEAD_MS = 10_000
# The client asks for 60 s windows. This bounds how much of a file one request can make ffmpeg read.
MAX_WINDOW_MS = 300_000


@dataclass(frozen=True)
class Discovery:
    """What one file offers: its ASS tracks as the client lists them, and its font attachments by
    stream index, each with the media type it is served as."""

    tracks: tuple[dict, ...] = ()
    fonts: dict[int, str] = field(default_factory=dict)

    def answer(self, defer_fonts: bool = False) -> dict:
        """The body of GET /embedded-ass. With deferFonts=1, `fonts` is null even when the file has
        none, as stock answers it: the client then asks /embedded-ass/fonts if it got tracks."""
        return {"tracks": [dict(t) for t in self.tracks],
                "fonts": None if defer_fonts else self.font_list()}

    def font_list(self) -> list[dict]:
        """The body of GET /embedded-ass/fonts, and the discovery answer's `fonts`."""
        return [{"id": i} for i in self.fonts]

    def stream_index(self, number: int) -> int | None:
        """The ffprobe stream index of the ASS track the client names `number`, or None."""
        if any(t["number"] == number for t in self.tracks):
            return number - 1
        return None


def _tags(stream: dict) -> dict:
    """A stream's tags with lower-case keys (ffprobe keeps whatever case the container used)."""
    tags = stream.get("tags")
    return {str(k).lower(): v for k, v in tags.items()} if isinstance(tags, dict) else {}


def _mime(stream: dict) -> str:
    return str(_tags(stream).get("mimetype") or "").split(";")[0].strip().lower()


def is_font_attachment(stream: dict) -> bool:
    """The client's test on one ffprobe stream: an attachment whose MIME type is a font type, or
    whose file name ends in a font extension."""
    if stream.get("codec_type") != "attachment":
        return False
    return _mime(stream) in FONT_MIME_TYPES or bool(
        _FONT_EXT.search(str(_tags(stream).get("filename") or "")))


def track_label(stream: dict) -> str:
    """The track's title, else its language, else its number -- then " (styled)".

    The TV's menu also lists the player's own native, unstyled entry for the same track, and the
    suffix is what tells the two apart."""
    tags = _tags(stream)
    name = (str(tags.get("title") or "").strip() or str(tags.get("language") or "").strip()
            or f"Track {track_number(stream)}")
    return f"{name} (styled)"


def track_number(stream: dict) -> int:
    """The number a track is offered by: its Matroska TrackNumber, as stock 4.22.0 numbers it.

    stremio-video 0.0.99 pairs `EMBEDDED_ASS_<number>` with the TV's own entry for the same track,
    whose number it takes from the stock server, and hides one of the two. ffprobe 4.4 does not
    report TrackNumber, but muxers write it from 1 in track order, which is ffprobe's stream order:
    the stream index + 1. The client's Tizen code makes the same assumption (`id - 1` is the
    player's own track index)."""
    return stream["index"] + 1


def discover(ffprobe_json: dict) -> Discovery:
    """Map `ffprobe -show_streams` output to what the client is offered.

    A track's `number` is track_number(); `Discovery.stream_index` turns it back into the stream
    index ffmpeg's `-map 0:<n>` takes. A font's `id` is its stream index, which
    `-dump_attachment:<n>` takes as it is: the client only ever hands it back."""
    tracks: list[dict] = []
    fonts: dict[int, str] = {}
    for s in ffprobe_json.get("streams") or []:
        if not isinstance(s, dict) or not isinstance(s.get("index"), int):
            continue
        codec = str(s.get("codec_name") or "").lower()
        if s.get("codec_type") == "subtitle" and codec in ASS_CODECS:
            lang = str(_tags(s).get("language") or "").strip() or "und"
            tracks.append({"number": track_number(s), "codec": codec, "lang": lang,
                           "label": track_label(s)})
        elif is_font_attachment(s):
            mime = _mime(s)
            fonts[s["index"]] = mime if mime in FONT_MIME_TYPES else "application/octet-stream"
    return Discovery(tuple(tracks), fonts)


def parse_window(from_q: str | None, to_q: str | None) -> tuple[int, int] | None:
    """`from` and `to` as the client sends them (whole milliseconds), or None when they are not a
    window this server extracts: missing, not a non-negative integer, empty or reversed, or longer
    than MAX_WINDOW_MS."""
    if from_q is None or to_q is None or not from_q.isdecimal() or not to_q.isdecimal():
        return None
    start, end = int(from_q), int(to_q)
    if end <= start or end - start > MAX_WINDOW_MS:
        return None
    return start, end


def _secs(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def probe_argv(url: str) -> list[str]:
    return ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format",
            url]


def window_argv(url: str, index: int, from_ms: int, to_ms: int) -> list[str]:
    """ffmpeg for one window of the track at stream `index`: its header and styles, then its events
    from LEAD_MS before `from_ms` up to `to_ms`, at the file's own times.

    `-copyts` keeps the times absolute, which the client needs: it swaps each window in whole
    (`assRenderer.setTrack`) and draws it against the video's own clock. With `-copyts` the end
    must be `-to`, an absolute time. `-t 60` counts from zero instead, and returned no events at
    all when measured against the image's ffmpeg 4.4.1."""
    return ["ffmpeg", "-hide_banner", "-v", "error", "-copyts",
            "-ss", _secs(max(0, from_ms - LEAD_MS)), "-i", url,
            "-map", f"0:{index}", "-c:s", "copy", "-to", _secs(to_ms), "-f", "ass", "pipe:1"]


def font_dump_argv(url: str, ids: list[int], out_dir: str) -> list[str]:
    """ffmpeg that writes each font attachment `<id>` to `<out_dir>/<id>.font`, all in one pass.

    Files are named by stream index. An attachment's own `filename` tag comes from the torrent, so
    it is never used as a path."""
    argv = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    for i in ids:
        argv += [f"-dump_attachment:{i}", os.path.join(out_dir, f"{i}.font")]
    return [*argv, "-i", url, "-t", "0", "-f", "null", "-"]
