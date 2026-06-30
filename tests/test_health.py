from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_health_ok():
    c = TestClient(create_app())
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("healthy", "degraded", "unhealthy")
    assert "components" in body


def test_health_reports_version():
    from stremiosrv.app import create_app
    from starlette.testclient import TestClient
    body = TestClient(create_app()).get("/health").json()
    assert "version" in body  # running server version for the admin Software Updates card
