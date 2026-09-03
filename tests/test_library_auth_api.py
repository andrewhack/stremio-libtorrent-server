import pytest
from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.config import Settings
from stremiosrv.library import api as lib
from stremiosrv.library import stremio_api

HTTPS = {"X-Forwarded-Proto": "https"}
USER = {"_id": "user-1", "email": "owner@example.com"}


def _client(tmp_path, **kw):
    # base_url https so the Secure cookie is actually stored by the test client — over http it
    # would be silently dropped and every session assertion below would test nothing.
    s = Settings(library_ui=True, cache_root=str(tmp_path), **kw)
    return TestClient(create_app(settings=s), base_url="https://testserver")


@pytest.fixture(autouse=True)
def _own_cert(monkeypatch):
    """Default to a bring-your-own cert; the shared-cert case overrides this explicitly."""
    monkeypatch.setattr(lib.certcheck, "cert_san", lambda p: "DNS:stremio.example.com")


@pytest.fixture(autouse=True)
def _fresh_limiter():
    """The limiter is module state shared by every test in the process; without this, tests run
    after the rate-limit test would start already throttled."""
    lib._login_limiter._hits.clear()


@pytest.fixture()
def client(tmp_path):
    return _client(tmp_path)


def test_session_requires_a_valid_key(client, monkeypatch):
    def boom(key, **kw):
        raise stremio_api.StremioApiError(1, "Session does not exist")
    monkeypatch.setattr(lib.stremio_api, "get_user", boom)
    r = client.post("/library/api/session", json={"authKey": "bad"}, headers=HTTPS)
    assert r.status_code == 401
    assert lib.COOKIE not in r.cookies


def test_session_issues_a_cookie(client, monkeypatch):
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    r = client.post("/library/api/session", json={"authKey": "good"}, headers=HTTPS)
    assert r.status_code == 200
    assert r.json()["user"]["_id"] == "user-1"
    assert lib.COOKIE in r.cookies


def test_second_account_is_refused(client, monkeypatch):
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    client.post("/library/api/session", json={"authKey": "good"}, headers=HTTPS)
    monkeypatch.setattr(lib.stremio_api, "get_user",
                        lambda key, **kw: {"_id": "user-2", "email": "x@y.z"})
    r = client.post("/library/api/session", json={"authKey": "also-valid"}, headers=HTTPS)
    assert r.status_code == 403


def test_state_requires_a_session(client):
    assert client.get("/library/api/state", headers=HTTPS).status_code == 401


def test_logout_invalidates(client, monkeypatch):
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    client.post("/library/api/session", json={"authKey": "good"}, headers=HTTPS)
    assert client.get("/library/api/state", headers=HTTPS).status_code == 200
    client.delete("/library/api/session", headers=HTTPS)
    assert client.get("/library/api/state", headers=HTTPS).status_code == 401


def test_plain_http_is_refused(client, monkeypatch):
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    r = client.post("/library/api/session", json={"authKey": "good"},
                    headers={"X-Forwarded-Proto": "http"})
    assert r.status_code == 400


def test_plain_http_allowed_when_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    c = _client(tmp_path, library_allow_http=True)
    r = c.post("/library/api/session", json={"authKey": "good"},
               headers={"X-Forwarded-Proto": "http"})
    assert r.status_code == 200


def test_password_login_blocked_on_the_shared_cert(tmp_path, monkeypatch):
    monkeypatch.setattr(lib.certcheck, "cert_san",
                        lambda p: "DNS:*.519b6502d940.stremio.rocks")
    c = _client(tmp_path)
    r = c.post("/library/api/login",
               json={"email": "a@b.c", "password": "pw"}, headers=HTTPS)
    assert r.status_code == 409
    assert c.get("/library/api/config", headers=HTTPS).json()["passwordLogin"] is False


def test_password_login_never_reaches_stremio_on_the_shared_cert(tmp_path, monkeypatch):
    """The refusal must happen BEFORE the relay. Returning 409 after already forwarding the
    password would leak it to the network the gate exists to protect it from."""
    monkeypatch.setattr(lib.certcheck, "cert_san",
                        lambda p: "DNS:*.519b6502d940.stremio.rocks")
    called = []
    monkeypatch.setattr(lib.stremio_api, "login",
                        lambda e, p, **kw: called.append(1) or {"authKey": "AK", "user": USER})
    c = _client(tmp_path)
    c.post("/library/api/login", json={"email": "a@b.c", "password": "pw"}, headers=HTTPS)
    assert called == [], "password was relayed to api.strem.io despite the shared-cert refusal"


def test_password_login_returns_the_authkey_for_addon_calls(client, monkeypatch):
    monkeypatch.setattr(lib.stremio_api, "login",
                        lambda e, p, **kw: {"authKey": "AK", "user": USER})
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    r = client.post("/library/api/login",
                    json={"email": "a@b.c", "password": "pw"}, headers=HTTPS)
    assert r.status_code == 200
    assert r.json()["authKey"] == "AK"


def test_login_is_rate_limited(client, monkeypatch):
    def boom(e, p, **kw):
        raise stremio_api.StremioApiError(2, "wrong password")
    monkeypatch.setattr(lib.stremio_api, "login", boom)
    codes = [client.post("/library/api/login",
                         json={"email": "a@b.c", "password": "x"},
                         headers=HTTPS).status_code for _ in range(7)]
    assert 429 in codes


def test_failure_reason_is_not_disclosed(client, monkeypatch):
    """api.strem.io answers a wrong email with `wrongEmail: true` — an account-enumeration oracle.
    Our box must not re-export it: every failure looks the same from outside."""
    def boom(e, p, **kw):
        raise stremio_api.StremioApiError(2, "User not found")
    monkeypatch.setattr(lib.stremio_api, "login", boom)
    body = client.post("/library/api/login",
                       json={"email": "a@b.c", "password": "x"}, headers=HTTPS).text
    assert "User not found" not in body and "wrongEmail" not in body


def test_password_is_never_logged(client, monkeypatch, caplog):
    def boom(e, p, **kw):
        raise stremio_api.StremioApiError(2, "wrong password")
    monkeypatch.setattr(lib.stremio_api, "login", boom)
    client.post("/library/api/login",
                json={"email": "a@b.c", "password": "hunter2"}, headers=HTTPS)
    assert "hunter2" not in caplog.text


def test_cross_origin_post_is_rejected(client, monkeypatch):
    """The session is a cookie on an internet-facing origin. Without this, a page the owner merely
    visits could POST to their box and act as them."""
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    r = client.post("/library/api/session", json={"authKey": "good"},
                    headers={**HTTPS, "Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_same_origin_post_is_allowed(client, monkeypatch):
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    r = client.post("/library/api/session", json={"authKey": "good"},
                    headers={**HTTPS, "Origin": "https://testserver"})
    assert r.status_code == 200


def test_originless_post_is_allowed(client, monkeypatch):
    """curl and the appliance TUI send no Origin; only a cross-origin BROWSER post is the vector."""
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    assert client.post("/library/api/session", json={"authKey": "good"},
                       headers=HTTPS).status_code == 200


def test_cross_origin_is_rejected_on_state_changing_routes_too(client, monkeypatch):
    """require_session guards the rest of the API; the CSRF check must be inside it, not bolted
    onto the auth handlers alone."""
    monkeypatch.setattr(lib.stremio_api, "get_user", lambda key, **kw: USER)
    client.post("/library/api/session", json={"authKey": "good"}, headers=HTTPS)
    r = client.delete("/library/api/session",
                      headers={**HTTPS, "Origin": "https://evil.example.com"})
    assert r.status_code == 403
