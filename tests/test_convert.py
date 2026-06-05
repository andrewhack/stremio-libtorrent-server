from stremiosrv.subs.convert import srt_to_vtt

SRT = "1\n00:00:01,000 --> 00:00:04,000\nHello world\n\n2\n00:00:05,500 --> 00:00:06,000\nBye\n"


def test_adds_webvtt_header():
    out = srt_to_vtt(SRT)
    assert out.startswith("WEBVTT\n\n")


def test_converts_comma_to_dot_in_timestamps():
    out = srt_to_vtt(SRT)
    assert "00:00:01.000 --> 00:00:04.000" in out
    assert "00:00:05.500 --> 00:00:06.000" in out
    assert ",000" not in out


def test_normalizes_crlf():
    out = srt_to_vtt("1\r\n00:00:01,000 --> 00:00:02,000\r\nHi\r\n")
    assert "\r" not in out
