"""SRT -> WebVTT conversion. Pure (str -> str), unit-testable."""
from __future__ import annotations

import re

_TS = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


def srt_to_vtt(text: str) -> str:
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    body = _TS.sub(r"\1.\2", body)  # comma -> dot in timestamps
    return "WEBVTT\n\n" + body.strip() + "\n"
