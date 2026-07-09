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


def test_opensub_hash_route(tmp_path):
    p = tmp_path / "v.bin"
    p.write_bytes(b"\x00" * (2 * 65536))  # 128 KiB zeros -> filesize hash
    c = TestClient(create_app())
    r = c.get("/opensubHash", params={"videoUrl": str(p)})
    assert r.status_code == 200
    # Stock-server envelope: result carries BOTH the hash AND the byte size. OpenSubtitles matches on
    # moviehash + moviebytesize, so a bare hash (no size) silently breaks OpenSubtitles-addon matching.
    assert r.json() == {"error": None, "result": {"size": 131072, "hash": "0000000000020000"}}


def test_opensub_hash_requires_source():
    c = TestClient(create_app())
    r = c.get("/opensubHash")
    assert r.status_code == 422


def test_casting_returns_empty_list():
    c = TestClient(create_app())
    r = c.get("/casting")
    assert r.status_code == 200
    assert r.json() == []
