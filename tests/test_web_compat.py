"""Web-player compatibility: stream Content-Type + external subtitle proxy (v0.2.2)."""
from fastapi.testclient import TestClient

from stremiosrv.api import subs
from stremiosrv.api.subs import srt_to_vtt
from stremiosrv.app import create_app
from stremiosrv.stream.fileserver import content_type_for


class _FakeResp:
    """Minimal stand-in for the http.client.HTTPResponse upstream.open_url returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default: str = "") -> str:
        return default

    def close(self) -> None:
        pass


class _FakeConn:
    """Minimal stand-in for the connection upstream.open_url returns alongside the response."""

    def close(self) -> None:
        pass


def test_content_type_known_containers():
    assert content_type_for("Big Buck Bunny.mp4") == "video/mp4"
    assert content_type_for("show.S01E01.mkv") == "video/x-matroska"
    assert content_type_for("clip.webm") == "video/webm"


def test_content_type_unknown_falls_back():
    assert content_type_for("file.weirdext") == "application/octet-stream"


def test_srt_to_vtt_converts_timestamps_and_header():
    out = srt_to_vtt("1\n00:00:01,000 --> 00:00:02,500\nHello\n")
    assert out.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in out


def test_srt_to_vtt_passes_through_existing_vtt():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"
    assert srt_to_vtt(vtt) == vtt


def test_subtitles_proxy_rejects_non_http_scheme():
    # SSRF guard: only http(s) sources allowed (no file://, etc.).
    client = TestClient(create_app(engine=None))
    r = client.get("/subtitles.vtt", params={"from": "file:///etc/passwd"})
    assert r.status_code == 400


def test_subtitles_proxy_srt_serves_subrip_with_browser_ua(monkeypatch):
    # Android/native players request `.srt`; it must be served (not fall through to the web-player
    # HTML), and the fetch must send a browser UA (subs5.strem.io 403s the default urllib agent).
    seen = {}

    def fake_open_url(url, method, headers, home_client, deadline):
        seen["ua"] = headers.get("user-agent")
        seen["url"] = url
        return _FakeResp(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"), _FakeConn()

    monkeypatch.setattr(subs.upstream, "open_url", fake_open_url)
    client = TestClient(create_app(engine=None))
    r = client.get("/subtitles.srt", params={"from": "https://subs5.strem.io/en/download/x"})
    assert r.status_code == 200
    assert "application/x-subrip" in r.headers["content-type"]
    assert "00:00:01,000" in r.text  # SubRip preserved (comma), NOT converted to WebVTT
    assert seen["ua"] and "Mozilla" in seen["ua"]
    assert seen["url"] == "https://subs5.strem.io/en/download/x"


def test_subtitles_proxy_vtt_still_normalizes_to_webvtt(monkeypatch):
    monkeypatch.setattr(
        subs.upstream, "open_url",
        lambda url, method, headers, home_client, deadline: (
            _FakeResp(b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"), _FakeConn()),
    )
    client = TestClient(create_app(engine=None))
    r = client.get("/subtitles.vtt", params={"from": "https://host/sub"})
    assert r.status_code == 200
    assert "text/vtt" in r.headers["content-type"]
    assert r.text.lstrip().startswith("WEBVTT")
