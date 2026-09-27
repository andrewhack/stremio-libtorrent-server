import os

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
    c = TestClient(create_app())
    with caplog.at_level(logging.WARNING):
        r = c.get("/" + "a" * 40 + "/0/subtitles.json", params={"mediaURL": "http://x/y"})
    assert r.status_code == 200
    assert r.json() == {"subtitles": []}
    assert "timed out" in caplog.text


def test_subtitles_from_a_refused_destination_is_403():
    c = TestClient(create_app())
    # link-local (cloud-metadata range) is refused to everyone, home or not
    r = c.get("/subtitles.srt", params={"from": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 403


def test_subtitles_from_non_http_is_400():
    c = TestClient(create_app())
    r = c.get("/subtitles.vtt", params={"from": "file:///etc/hostname"})
    assert r.status_code == 400
