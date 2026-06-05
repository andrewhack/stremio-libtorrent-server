from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_opensub_hash_route(tmp_path):
    p = tmp_path / "v.bin"
    p.write_bytes(b"\x00" * (2 * 65536))  # 128 KiB zeros -> filesize hash
    c = TestClient(create_app())
    r = c.get("/opensubHash", params={"videoUrl": str(p)})
    assert r.status_code == 200
    assert r.json()["result"] == "0000000000020000"


def test_opensub_hash_requires_source():
    c = TestClient(create_app())
    r = c.get("/opensubHash")
    assert r.status_code == 422


def test_casting_returns_empty_list():
    c = TestClient(create_app())
    r = c.get("/casting")
    assert r.status_code == 200
    assert r.json() == []
