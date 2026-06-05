from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_settings_shape():
    c = TestClient(create_app())
    b = c.get("/settings").json()
    assert set(b) >= {"options", "values", "baseUrl"}
    assert "btMaxConnections" in b["values"]
    assert b["values"]["cacheRoot"].endswith(".stremio-server")


def test_network_and_device_info():
    c = TestClient(create_app())
    assert "availableInterfaces" in c.get("/network-info").json()
    assert "availableHardwareAccelerations" in c.get("/device-info").json()


def test_global_stats_empty():
    c = TestClient(create_app())
    assert c.get("/stats.json").json() == {}
