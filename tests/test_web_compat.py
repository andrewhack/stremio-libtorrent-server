"""Web-player compatibility: stream Content-Type + external subtitle proxy (v0.2.2)."""
from fastapi.testclient import TestClient

from stremiosrv.api import subs
from stremiosrv.api.subs import srt_to_vtt
from stremiosrv.app import create_app
from stremiosrv.stream.fileserver import content_type_for


class _FakeResp:
    """Minimal stand-in for urllib's response (context manager + read + headers)."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers = {"Content-Encoding": ""}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        return _FakeResp(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")

    monkeypatch.setattr(subs.urllib.request, "urlopen", fake_urlopen)
    client = TestClient(create_app(engine=None))
    r = client.get("/subtitles.srt", params={"from": "https://subs5.strem.io/en/download/x"})
    assert r.status_code == 200
    assert "application/x-subrip" in r.headers["content-type"]
    assert "00:00:01,000" in r.text  # SubRip preserved (comma), NOT converted to WebVTT
    assert seen["ua"] and "Mozilla" in seen["ua"]
    assert seen["url"] == "https://subs5.strem.io/en/download/x"


def test_subtitles_proxy_vtt_still_normalizes_to_webvtt(monkeypatch):
    monkeypatch.setattr(
        subs.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"),
    )
    client = TestClient(create_app(engine=None))
    r = client.get("/subtitles.vtt", params={"from": "https://host/sub"})
    assert r.status_code == 200
    assert "text/vtt" in r.headers["content-type"]
    assert r.text.lstrip().startswith("WEBVTT")
