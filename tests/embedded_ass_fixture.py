"""A small MKV built with the real ffmpeg, for the embedded-ASS tests that need one.

90 s of test pattern, then (by stream index):
  1  an ASS track titled "Signs" in English, one "line N" event every 2 s, each shown for 1.5 s
  2  a SubRip track, which the TV route must NOT offer
  3  a .ttf attachment with a font MIME type
  4  a .otf attachment typed application/vnd.ms-opentype, a real OTF type the client's list leaves
     out, so it counts as a font by its extension alone
  5  a .txt attachment, which is no font
The "fonts" are random bytes: the tests compare bytes, they never render.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DURATION = 90
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg and ffprobe on PATH")


def _ts(s: float) -> str:
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def build(dir_: Path) -> dict:
    """Write the fixture into `dir_`. Returns its path and the bytes of each attachment by index."""
    ass = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 320", "PlayResY: 180", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Fixture Sans,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,"
        "100,0,0,1,1,0,2,10,10,10,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ass += [f"Dialogue: 0,{_ts(t)},{_ts(t + 1.5)},Default,,0,0,0,,line {t}"
            for t in range(0, DURATION, 2)]
    (dir_ / "fx.ass").write_text("\n".join(ass) + "\n", encoding="utf-8")
    (dir_ / "fx.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nplain\n", encoding="utf-8")
    attachments = {3: "fx.ttf", 4: "fx.otf", 5: "notes.txt"}
    data = {i: os.urandom(64 * 1024) for i in attachments}
    for i, name in attachments.items():
        (dir_ / name).write_bytes(data[i])
    out = dir_ / "fixture.mkv"
    argv = ["ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=24:duration={DURATION}",
            "-i", str(dir_ / "fx.ass"), "-i", str(dir_ / "fx.srt")]
    for name in attachments.values():
        argv += ["-attach", str(dir_ / name)]
    argv += ["-map", "0:v", "-map", "1:s", "-map", "2:s",
             "-c:v", "mpeg4", "-g", "48", "-c:s", "copy",
             "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Signs",
             "-metadata:s:t:0", "mimetype=application/x-truetype-font",
             "-metadata:s:t:1", "mimetype=application/vnd.ms-opentype",
             "-metadata:s:t:2", "mimetype=text/plain",
             str(out)]
    subprocess.run(argv, check=True, capture_output=True, timeout=120)
    return {"path": out, "attachments": data}
