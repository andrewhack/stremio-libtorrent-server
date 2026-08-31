from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.config import Settings


def _app(**kw):
    return create_app(settings=Settings(**kw))


def test_library_absent_when_flag_off():
    c = TestClient(_app(library_ui=False))
    assert c.get("/library/").status_code == 404
    assert c.get("/library/api/state").status_code == 404


def test_library_page_served_when_flag_on():
    c = TestClient(_app(library_ui=True))
    r = c.get("/library/", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_flag_defaults_off():
    assert Settings().library_ui is False
