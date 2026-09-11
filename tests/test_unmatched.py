"""A request no route answers must be counted -- by shape, never by content."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stremiosrv import unmatched
from stremiosrv.app import create_app
from stremiosrv.config import Settings

IH = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _clean():
    unmatched.reset()
    yield
    unmatched.reset()


@pytest.mark.parametrize(("method", "path", "want"), [
    ("GET", "/", "GET /"),
    ("GET", "/proxy/d=https%3A%2F%2Fexample.com/x.mp4", "GET /proxy/*"),
    ("POST", "/create", "POST /create"),
    ("GET", "/rar/create?lz=abc", "GET /rar/*"),
    ("HEAD", f"/{IH}/Some.Title.2009.mkv", "HEAD /{ih}/{name}"),
    ("GET", f"/{IH}/3", "GET /{ih}/{idx}"),
    ("GET", f"/{IH}/-1", "GET /{ih}/-1"),
    ("GET", f"/{IH}/0/Some.Title.mkv", "GET /{ih}/{idx}/*"),
    ("GET", "/thumb.jpg", "GET /thumb.jpg"),
    ("GET", "/Some.Title.2009.1080p.mkv", "GET /{x}"),
    ("GET", "/%F0%9F%8E%AC", "GET /{x}"),
    ("GET", "/aB3xR9k2Qz", "GET /{x}"),     # word-shaped, and still possibly somebody's token
    ("GET", "/wp-login.php", "GET /{x}"),   # a scanner's probe is not a route family
    ("BREW", "/heartbeat", "OTHER /heartbeat"),
    ("get", "/heartbeat?x=1", "GET /heartbeat"),
])
def test_shape_keeps_the_route_and_drops_every_value(method, path, want):
    assert unmatched.shape(method, path) == want


def test_shape_never_carries_an_infohash_a_name_or_a_query():
    s = unmatched.shape("GET", f"/{IH}/My.Home.Video.mkv?tr=udp%3A%2F%2Ftracker&f=x")
    assert IH not in s and "Video" not in s and "tracker" not in s


def test_an_unknown_path_is_404_and_counted():
    c = TestClient(create_app())
    assert c.get("/definitely-not-a-route").status_code == 404
    assert c.get("/stats.json").json()["unmatchedRoutes"] == {"GET /{x}": 1}


def test_an_unimplemented_method_is_counted():
    """How an unbuilt `POST /settings` shows up."""
    c = TestClient(create_app())
    assert c.post("/settings", json={}).status_code == 405
    assert unmatched.snapshot() == {"POST /settings": 1}


def test_a_routes_own_404_is_not_counted():
    """The library addon's guard answers a wrong token with a 404 on purpose -- a route answering,
    not a missing one."""
    c = TestClient(create_app(settings=Settings(library_ui=True)))
    assert c.get("/library/addon/not-the-token/manifest.json").status_code == 404
    assert unmatched.snapshot() == {}


def test_a_known_route_is_not_counted():
    c = TestClient(create_app())
    assert c.get("/health").status_code == 200
    assert c.get(f"/{IH}/stats.json").status_code == 200
    assert unmatched.snapshot() == {}


def test_nginx_fallback_is_counted_by_the_original_request():
    """nginx rewrites every unmatched path to /_unmatched and names the original in two headers."""
    c = TestClient(create_app(), client=("127.0.0.1", 50000))
    r = c.get("/_unmatched", headers={"X-Original-Method": "POST",
                                      "X-Original-URI": "/rar/create?lz=abc"})
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}
    assert unmatched.snapshot() == {"POST /rar/*": 1}


def test_fallback_headers_from_anyone_but_nginx_are_not_believed():
    c = TestClient(create_app(), client=("192.168.1.20", 50000))
    c.get("/_unmatched", headers={"X-Original-Method": "POST", "X-Original-URI": "/create"})
    assert unmatched.snapshot() == {"GET /_unmatched": 1}


def test_shapes_are_capped(monkeypatch):
    monkeypatch.setattr(unmatched, "MAX_SHAPES", 2)
    for p in ("/heartbeat", "/yt", "/rar", "/zip"):
        unmatched.record("GET", p)
    assert unmatched.snapshot() == {"GET /heartbeat": 1, "GET /yt": 1, "other": 2}


def test_first_sighting_is_logged_once_without_the_path(caplog):
    caplog.set_level("INFO", logger="stremiosrv.unmatched")
    unmatched.record("GET", f"/{IH}/Some.Title.mkv")
    unmatched.record("GET", f"/{IH}/Other.Title.mkv")
    lines = [r.getMessage() for r in caplog.records if r.name == "stremiosrv.unmatched"]
    assert lines == ["no route for GET /{ih}/{name} -- counted in /stats.json unmatchedRoutes"]
