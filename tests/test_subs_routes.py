import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from stremiosrv.api.subs import parse_stream_url
from stremiosrv.app import create_app


def test_parse_stream_url():
    assert parse_stream_url("https://h:12470/" + "a" * 40 + "/6?") == ("a" * 40, 6)
    assert parse_stream_url("/tmp/movie.mkv") is None


def test_opensub_hash_null_for_unresolvable_url():
    # a stream URL with no engine -> {"error": null, "result": null}, NOT a 500
    c = TestClient(create_app())
    r = c.get("/opensubHash", params={"videoUrl": "https://h:12470/" + "a" * 40 + "/6"})
    assert r.status_code == 200
    assert r.json() == {"error": None, "result": None}


def test_opensub_hash_requires_source():
    c = TestClient(create_app())
    r = c.get("/opensubHash")
    assert r.status_code == 422


def test_opensubhash_does_not_probe_arbitrary_local_paths(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"x" * 1000)
    c = TestClient(create_app())
    r = c.get("/opensubHash", params={"videoUrl": str(secret)})
    assert r.status_code == 200
    assert r.json() == {"error": None, "result": None}  # never a size/hash for a local path


def test_opensubhash_rejects_a_bare_existing_directory():
    c = TestClient(create_app())
    r = c.get("/opensubHash", params={"videoUrl": os.getcwd()})
    assert r.json() == {"error": None, "result": None}


def test_opensub_hash_returns_size_and_hash_on_engine_success(monkeypatch):
    # The engine-success envelope: a resolvable own stream URL, metadata present and the edge pieces
    # in, returns {"error": null, "result": {"size", "hash"}} -- the shape the OpenSubtitles addon
    # needs (moviehash AND moviebytesize). The null cases are covered above; this covers the hit.
    from stremiosrv.api import subs

    class FakeHandle:
        def has_metadata(self):
            return True

    class FakeEngine:
        def get(self, info_hash):
            return FakeHandle()

        def add(self, info_hash):
            return FakeHandle()

        def save_path(self):
            return "/data"

    monkeypatch.setattr(subs, "_ensure_edges", lambda *a, **k: True)
    monkeypatch.setattr(subs, "file_disk_path", lambda *a, **k: "/data/movie.mkv")
    monkeypatch.setattr(subs, "opensubtitles_hash_and_size", lambda path: ("deadbeefdeadbeef", 4242))
    app = create_app()
    app.state.engine = FakeEngine()
    r = TestClient(app).get("/opensubHash", params={"videoUrl": "https://h:12470/" + "a" * 40 + "/6"})
    assert r.status_code == 200
    assert r.json() == {"error": None, "result": {"size": 4242, "hash": "deadbeefdeadbeef"}}


def test_casting_returns_empty_list():
    c = TestClient(create_app())
    r = c.get("/casting")
    assert r.status_code == 200
    assert r.json() == []


# --- /subtitleSignature: stremio-video >= 0.0.93 calls this at every load whose probe does not rule
# out an embedded subtitle track. We answer the envelope with a null signature on purpose — the
# reference server.js v4.21.1 has no such route (404) and nothing upstream consumes the value, so
# there is no algorithm to implement and a made-up string would be *used* the day a consumer ships.


def test_subtitle_signature_envelope():
    """The exact shape stremio-video parses: resp.error falsy, resp.result.signature present."""
    c = TestClient(create_app())
    r = c.get("/subtitleSignature", params={"videoUrl": "http://x/y"})
    assert r.status_code == 200
    b = r.json()
    assert b["error"] is None
    assert b["result"] == {"signature": None}


def test_subtitle_signature_maps_to_null_in_the_client():
    """Mirror of fetchEmbeddedSubtitleSignature's own expression, so the contract is asserted the
    way the client evaluates it rather than the way we happen to serialise it."""
    c = TestClient(create_app())
    b = c.get("/subtitleSignature", params={"videoUrl": "http://x/y"}).json()
    signature = b["result"]["signature"] if b.get("result") and isinstance(
        b["result"].get("signature"), str) else None
    assert signature is None


def test_subtitle_signature_accepts_the_container_hint():
    c = TestClient(create_app())
    r = c.get("/subtitleSignature", params={"videoUrl": "http://x/y", "container": "matroska,webm"})
    assert r.status_code == 200


def test_subtitle_signature_requires_video_url():
    c = TestClient(create_app())
    assert c.get("/subtitleSignature").status_code == 422


def test_subtitle_signature_never_probes():
    """The reason it is cheap. probe_media() shells out to ffprobe uncached, and this is called at
    playback start on the box that is serving the stream."""
    import stremiosrv.api.subs as subs_api

    calls = []
    original = subs_api.probe_media
    subs_api.probe_media = lambda *a, **k: calls.append(a) or {"format": {}, "streams": []}
    try:
        TestClient(create_app()).get("/subtitleSignature", params={"videoUrl": "http://x/y"})
    finally:
        subs_api.probe_media = original
    assert calls == []


def test_subtitle_signature_is_counted():
    from stremiosrv import metrics

    metrics.reset()
    c = TestClient(create_app())
    c.get("/subtitleSignature", params={"videoUrl": "http://x/y"})
    c.get("/subtitleSignature", params={"videoUrl": "http://x/z"})
    c.get("/subtitleSignature")  # 422, not an ask we could answer
    assert metrics.playback_stats()["subtitleSignatureAsks"] == 2
    metrics.reset()


def test_subtitle_signature_does_not_shadow_other_routes():
    """One-segment literal, registered in the same router as /subtitles.{ext} and after the
    /{info_hash}/... routes. Assert the neighbours still resolve to themselves."""
    c = TestClient(create_app())
    assert c.get("/subtitleSignature", params={"videoUrl": "http://x/y"}).json()["result"] == {
        "signature": None}
    # /subtitles.srt still reaches the proxy route (422 = its own validation, not a 404/mismatch)
    assert c.get("/subtitles.srt").status_code in (422, 400)
    assert c.get("/opensubHash").status_code == 422


def test_subtitles_list_answers_its_empty_shape_when_the_probe_times_out(monkeypatch, caplog):
    """The player asks for this on every playback and it has an ordinary answer for "no tracks",
    so a slow ffprobe must not make it a 500. It is still logged: an empty list for a file that
    does have subtitles is otherwise a silent wrong answer."""
    import logging

    from stremiosrv.api import subs as subs_api
    from stremiosrv.transcode.probe import ProbeTimeoutError

    def _times_out(url):
        raise ProbeTimeoutError("ffprobe did not answer within 30s")

    monkeypatch.setattr(subs_api, "probe_media", _times_out)
    monkeypatch.setattr(subs_api, "resolve_media_input", lambda request, url: url)
    c = TestClient(create_app())
    with caplog.at_level(logging.WARNING):
        r = c.get("/" + "a" * 40 + "/0/subtitles.json", params={"mediaURL": "http://x/y"})
    assert r.status_code == 200
    assert r.json() == {"subtitles": []}
    assert "timed out" in caplog.text


def test_subtitles_list_resolves_the_media_url(monkeypatch):
    """probe_media must receive the resolved (own-or-reader) URL, never the raw client mediaURL --
    the same contract hls.py's probe route gets (Task 7 / Minor 8)."""
    from stremiosrv.api import subs as subs_api

    seen_by_resolve = []
    seen_by_probe = []

    def fake_resolve(request, url):
        seen_by_resolve.append(url)
        return "http://127.0.0.1:1/resolved"

    def fake_probe(url):
        seen_by_probe.append(url)
        return {"format": {"name": "matroska"}, "streams": []}

    monkeypatch.setattr(subs_api, "resolve_media_input", fake_resolve)
    monkeypatch.setattr(subs_api, "probe_media", fake_probe)
    c = TestClient(create_app())
    r = c.get("/" + "a" * 40 + "/0/subtitles.json",
              params={"mediaURL": "https://cdn.example/v.mkv"})
    assert r.status_code == 200
    assert seen_by_resolve == ["https://cdn.example/v.mkv"]
    assert seen_by_probe == ["http://127.0.0.1:1/resolved"]


def test_subtitles_list_refuses_hls_format_input():
    """A torrent whose bytes are themselves an HLS playlist must not be handed to ffprobe's HLS
    demuxer here either -- same Minor-8 concern as hls.py's probe/master, and the same fix: refuse
    a *successful* probe that reports an hls format. ProbeTimeoutError's separate
    `{"subtitles": []}` branch (a slow/failed probe) is untouched -- this is only for a probe that
    succeeded and found a playlist.

    For an own mediaURL (the normal torrent case), resolve_media_input returns it unchanged and
    ffprobe's HLS demuxer can then open absolute LAN segment URLs a malicious torrent's playlist
    names -- the protocol whitelist permits http/https, so it does not stop this on its own."""
    c = TestClient(create_app())
    with patch("stremiosrv.api.subs.resolve_media_input", side_effect=lambda r, u: u), \
         patch("stremiosrv.api.subs.probe_media",
               return_value={"format": {"name": "hls"}, "streams": [], "samples": {}}):
        r = c.get("/" + "a" * 40 + "/0/subtitles.json",
                  params={"mediaURL": "http://127.0.0.1:11470/aabb/0"})
    assert r.status_code == 415


def test_subtitles_vtt_resolves_the_media_url_and_whitelists_protocols(monkeypatch):
    """subtitles_vtt builds its own ffmpeg argv (it doesn't go through probe_media/build_hls_cmd),
    so both the resolve wiring and the protocol whitelist have to be proven here directly."""
    from stremiosrv.api import subs as subs_api

    seen = {}

    class P:
        returncode = 0
        stdout = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return P()

    monkeypatch.setattr(subs_api, "resolve_media_input",
                        lambda request, url: "http://127.0.0.1:1/resolved")
    monkeypatch.setattr(subs_api.subprocess, "run", fake_run)
    c = TestClient(create_app())
    r = c.get("/" + "a" * 40 + "/0/subtitles.vtt", params={"mediaURL": "https://cdn.example/v.mkv"})
    assert r.status_code == 200
    argv = seen["argv"]
    assert "http://127.0.0.1:1/resolved" in argv
    assert "https://cdn.example/v.mkv" not in argv  # the raw client URL never reaches ffmpeg
    assert "-protocol_whitelist" in argv
    i = argv.index("-protocol_whitelist")
    assert argv[i + 1] == "file,crypto,data,http,tcp,tls,https"
    assert i < argv.index("-i")


def test_subtitles_from_a_refused_destination_is_403():
    c = TestClient(create_app())
    # link-local (cloud-metadata range) is refused to everyone, home or not
    r = c.get("/subtitles.srt", params={"from": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 403


def test_subtitles_from_non_http_is_400():
    c = TestClient(create_app())
    r = c.get("/subtitles.vtt", params={"from": "file:///etc/hostname"})
    assert r.status_code == 400
