"""The /proxy options segment, as stremio-video builds it (URLSearchParams)."""
from __future__ import annotations

import pytest

from stremiosrv.proxy import opts


def test_parses_the_segment_stremio_video_builds():
    raw = ("d=https%3A%2F%2Fcdn.example.com%3A8443&h=User-Agent%3AMozilla%2F5.0"
           "&h=Referer%3Ahttps%3A%2F%2Fsite.example%2F&r=Content-Type%3Avideo%2Fmp4"
           "/movies/a%20b.mp4")
    o, path = opts.parse(raw)
    assert o.dest == "https://cdn.example.com:8443"
    assert o.req_headers == (("User-Agent", "Mozilla/5.0"), ("Referer", "https://site.example/"))
    assert o.res_headers == (("Content-Type", "video/mp4"),)
    assert path == "/movies/a%20b.mp4"


def test_a_path_inside_d_is_ignored_like_stock():
    o, path = opts.parse("d=https%3A%2F%2Fcdn.example%2Fignored/x.m3u8")
    assert o.dest == "https://cdn.example"
    assert path == "/x.m3u8"


def test_no_path_means_the_root():
    assert opts.parse("d=http%3A%2F%2Fcdn.example")[1] == "/"


@pytest.mark.parametrize("raw", [
    "h=A%3Ab/x", "d=ftp%3A%2F%2Fx/y", "d=file%3A%2F%2F%2Fetc/passwd", "d=/x", "",
])
def test_no_usable_destination_is_refused(raw):
    assert opts.parse(raw) is None


def test_a_header_that_would_inject_a_line_is_dropped():
    o, _ = opts.parse("d=http%3A%2F%2Fx&h=X-A%3A1%0D%0AX-Injected%3A2&h=X-B%3A2/p")
    assert o.req_headers == (("X-B", "2"),)


@pytest.mark.parametrize("spec", ["no-colon", ":value", "Bad Name:v", "Bad(Name:v",
                                  "X-Euro:€"])
def test_split_header_rejects_what_http_client_cannot_send(spec):
    assert opts.split_header(spec) is None


@pytest.mark.parametrize("raw", [
    "d=http%3A%2F%2Fexample.com%3A99999/x", "d=http%3A%2F%2Fexample.com%3A0/x",
    "d=http%3A%2F%2F%5B%3A%3A1/y", "d=http%3A%2F%2F%5Bnothost%5D/y",
])
def test_a_destination_that_cannot_be_requested_is_refused(raw):
    """A port out of range, port 0, a broken IPv6 literal: refused here, never a crash later."""
    assert opts.parse(raw) is None


def test_a_value_keeps_its_own_colons():
    assert opts.split_header("Referer:https://a.example:8080/x") == (
        "Referer", "https://a.example:8080/x")


def test_serialize_round_trips():
    o = opts.ProxyOpts("https://cdn.example", (("User-Agent", "A B/1.0"),),
                       (("Content-Type", "video/mp4"),))
    back, path = opts.parse(opts.serialize(o) + "/p")
    assert back == o
    assert path == "/p"
