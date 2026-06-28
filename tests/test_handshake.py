from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_settings_shape():
    c = TestClient(create_app())
    b = c.get("/settings").json()
    assert set(b) >= {"options", "values", "baseUrl"}
    assert "btMaxConnections" in b["values"]
    assert b["values"]["cacheRoot"].endswith(".stremio-server")


def test_base_url_reflects_request():
    c = TestClient(create_app())
    b = c.get("/settings", headers={"x-forwarded-proto": "https", "host": "example.com:12470"}).json()
    assert b["baseUrl"] == "https://example.com:12470"


def test_network_and_device_info():
    c = TestClient(create_app())
    assert "availableInterfaces" in c.get("/network-info").json()
    assert "availableHardwareAccelerations" in c.get("/device-info").json()


def test_global_stats_shape():
    c = TestClient(create_app())
    b = c.get("/stats.json").json()
    assert set(b) >= {"cache", "playback"}
    assert set(b["cache"]) >= {"cacheUsed", "cacheSize", "diskFree", "diskTotal"}
    assert set(b["playback"]) >= {"stalls", "stallSeconds", "timeouts"}
