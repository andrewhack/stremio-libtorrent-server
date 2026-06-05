from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_app_boots_and_health():
    c = TestClient(create_app())
    assert c.get("/health").status_code == 200


def test_cors_allows_all_origins():
    c = TestClient(create_app())
    r = c.get("/health", headers={"Origin": "https://app.strem.io"})
    assert r.headers.get("access-control-allow-origin") == "*"
