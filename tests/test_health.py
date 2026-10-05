import json

import pytest
from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.config import Settings


def test_health_ok():
    c = TestClient(create_app())
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("healthy", "degraded", "unhealthy")
    assert "components" in body


def test_health_reports_version():
    from starlette.testclient import TestClient

    from stremiosrv.app import create_app
    body = TestClient(create_app()).get("/health").json()
    assert "version" in body  # running server version for the admin Software Updates card


# --- the stremio.rocks certificate's status (docker/cert-status.sh writes the file) ---

WAITING = {"source": "self-signed", "state": "waiting", "reason": "refused",
           "detail": "General HTTPS Certificate DNS Error",
           "host": "192-168-5-124.519b6502d940.stremio.rocks",
           "lastTry": "2026-10-05T14:05:12Z", "nextTry": "2026-10-05T14:35:12Z",
           "writtenAt": "2026-10-05T14:05:12Z"}


def _health(tmp_path, status: dict | str | None):
    if status is not None:
        (tmp_path / "cert-status.json").write_text(
            status if isinstance(status, str) else json.dumps(status), encoding="utf-8")
    c = TestClient(create_app(settings=Settings(cache_root=str(tmp_path))))
    return c.get("/health")


def test_waiting_for_the_trusted_certificate_is_degraded(tmp_path):
    r = _health(tmp_path, WAITING)
    assert r.status_code == 503
    body = r.json()
    assert body["components"]["cert"] == "degraded"
    assert body["certStatus"] == {"source": "self-signed", "state": "waiting", "reason": "refused",
                                  "detail": "General HTTPS Certificate DNS Error",
                                  "nextTry": "2026-10-05T14:35:12Z"}


def test_renewing_is_reported_but_not_degraded(tmp_path):
    r = _health(tmp_path, {**WAITING, "source": "stremio.rocks", "state": "renewing"})
    assert r.status_code == 200 and r.json()["certStatus"]["state"] == "renewing"


def test_without_ipaddress_nothing_is_added(tmp_path):
    r = _health(tmp_path, {**WAITING, "source": "own", "state": "ok", "reason": None,
                           "detail": None, "host": None, "nextTry": None})
    assert r.status_code == 200 and "certStatus" not in r.json()


@pytest.mark.parametrize("broken", ['{"source": "self-signed", "state": "wai', "[]", "null"])
def test_a_broken_status_file_changes_nothing(tmp_path, broken):
    r = _health(tmp_path, broken)
    assert r.status_code == 200 and "certStatus" not in r.json()
