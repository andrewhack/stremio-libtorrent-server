"""Subtitle proxy decode/decompress — the charset + gzip handling that makes non-UTF-8 (Cyrillic)
OpenSubtitles render on strict players like ExoPlayer instead of coming up blank."""
import gzip

from stremiosrv.api.subs import _decompress, decode_subtitle, srt_to_vtt

CYRILLIC = "Здравей, Елза. Как си днес? Радвам се да те видя отново."


def test_decode_cyrillic_windows1251_not_mangled():
    # The bug: decode("utf-8", errors="replace") turns this into replacement junk -> blank on ExoPlayer.
    raw = CYRILLIC.encode("windows-1251")
    assert decode_subtitle(raw) == CYRILLIC


def test_decode_clean_utf8_passthrough():
    assert decode_subtitle("Héllo — привет".encode("utf-8")) == "Héllo — привет"


def test_decode_utf8_bom_stripped():
    assert decode_subtitle("﻿hi".encode("utf-8")) == "hi"


def test_decode_utf16():
    assert decode_subtitle("hi".encode("utf-16")) == "hi"


def test_decompress_gzip_by_header():
    body = b"1\n00:00:01,000 --> 00:00:02,000\nhi\n"
    assert _decompress(gzip.compress(body), "gzip") == body


def test_decompress_gzip_by_magic_bytes():
    body = b"1\n00:00:01,000 --> 00:00:02,000\nhi\n"
    assert _decompress(gzip.compress(body), "") == body  # no header, sniffed by 0x1f8b


def test_decompress_plain_passthrough():
    assert _decompress(b"WEBVTT\n\nplain", "") == b"WEBVTT\n\nplain"


def test_srt_to_vtt_timestamp_and_header():
    out = srt_to_vtt("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert out.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.000" in out


def test_to_webvtt_uses_ffmpeg_output(monkeypatch):
    from stremiosrv.api import subs

    class _Ok:
        returncode = 0
        stdout = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"

    monkeypatch.setattr(subs.subprocess, "run", lambda *a, **k: _Ok())
    out = subs.to_webvtt("[Script Info]\nDialogue: ...")  # ASS in -> ffmpeg gives clean VTT
    assert out.startswith("WEBVTT")
    assert "hi" in out


def test_to_webvtt_falls_back_when_ffmpeg_fails(monkeypatch):
    from stremiosrv.api import subs

    class _Fail:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(subs.subprocess, "run", lambda *a, **k: _Fail())
    out = subs.to_webvtt("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert out.startswith("WEBVTT")  # naive fallback still yields valid-ish VTT
    assert "00:00:01.000 --> 00:00:02.000" in out
