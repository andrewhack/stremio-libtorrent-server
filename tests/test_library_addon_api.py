"""The owner's view of the addon: where to install it from, and how to revoke it."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.config import Settings
from stremiosrv.library import api as lib
from stremiosrv.library import session as sessionmod

HTTPS = {"X-Forwarded-Proto": "https"}
USER = {"_id": "user-1", "email": "owner@example.com"}


def _client(tmp_path, **kw):
    # base_url https so the Secure cookie is actually stored by the test client -- see
    # test_library_auth_api.py's _client for why.
    s = Settings(library_ui=True, cache_root=str(tmp_path), **kw)
    return TestClient(create_app(settings=s), base_url="https://testserver")


def anonymous_client(tmp_path):
    return _client(tmp_path)


def signed_in_client(tmp_path):
    """A client with a live owner session.

    Neither test_library_auth_api.py nor test_library_session.py exposes this as a shared helper,
    so the authKey exchange test_library_auth_api.py's own tests use is copied inline here rather
    than adding a tests/helpers_library.py for the one file that would import it.
    """
    c = _client(tmp_path)
    with patch.object(lib.stremio_api, "get_user", lambda key, **kw: USER):
        r = c.post("/library/api/session", json={"authKey": "good"}, headers=HTTPS)
    assert r.status_code == 200
    return c


def test_the_install_url_carries_the_current_token_and_the_requests_origin(tmp_path):
    c = signed_in_client(tmp_path)
    r = c.get("/library/api/addon", headers={"Host": "box.invalid:12470",
                                             "X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    token = sessionmod.ensure_addon_token(str(tmp_path))
    assert r.json()["url"] == f"https://box.invalid:12470/library/addon/{token}/manifest.json"


def test_resetting_mints_a_new_token_so_old_installs_stop_working(tmp_path):
    c = signed_in_client(tmp_path)
    before = c.get("/library/api/addon").json()["url"]
    after = c.post("/library/api/addon/reset").json()["url"]
    assert after != before


def test_both_routes_need_a_session(tmp_path):
    c = anonymous_client(tmp_path)
    assert c.get("/library/api/addon").status_code in (401, 403)
    assert c.post("/library/api/addon/reset").status_code in (401, 403)
