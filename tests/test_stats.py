from fastapi.testclient import TestClient

from stremiosrv.app import create_app

ZERO = "0" * 40


def test_torrent_stats_null_when_absent():
    c = TestClient(create_app())
    assert c.get(f"/{ZERO}/stats.json").json() is None


def test_file_stats_null_when_absent():
    c = TestClient(create_app())
    assert c.get(f"/{ZERO}/0/stats.json").json() is None
