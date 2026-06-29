from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_settings_shape():
    c = TestClient(create_app())
    b = c.get("/settings").json()
    assert set(b) >= {"options", "values", "baseUrl"}
    assert "btMaxConnections" in b["values"]
    assert b["values"]["cacheRoot"].endswith(".stremio-server")
    # Must look like a real Stremio server version or native clients (desktop v6) reject us and
    # fall back to their bundled 127.0.0.1 server.
    assert b["values"]["serverVersion"] == "4.20.16"


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


def test_transcode_stats_no_gpu():
    c = TestClient(create_app())  # no converter, no profile -> CPU/direct-play only
    assert c.get("/transcode.json").json() == {"hwAccel": False, "profile": None, "activeTranscodes": 0}


def test_transcode_stats_with_profile_and_jobs():
    from stremiosrv.config import Settings

    class FakeConv:
        def active_count(self):
            return 2

    s = Settings()
    s.transcode_profile = "nvenc-linux"
    b = TestClient(create_app(settings=s, converter=FakeConv())).get("/transcode.json").json()
    assert b == {"hwAccel": True, "profile": "nvenc-linux", "activeTranscodes": 2}
