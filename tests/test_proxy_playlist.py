"""Playlists fetched through /proxy come back with every URL routed through it again."""
from __future__ import annotations

import pytest

from stremiosrv.proxy import opts, playlist

OPTS = opts.ProxyOpts("http://origin.example:8099", (("X-Token", "abc"),),
                      (("Content-Type", "video/mp2t"),))
ROOT = "/proxy/d=http%3A%2F%2Forigin.example%3A8099&h=X-Token%3Aabc&r=Content-Type%3Avideo%2Fmp2t"


def test_same_origin_absolute_and_root_relative_come_back_through_the_same_proxy():
    text = "http://origin.example:8099/a/seg1.ts?x=1\n/b/seg2.ts\n"
    assert playlist.rewrite(text, OPTS) == f"{ROOT}/a/seg1.ts?x=1\n{ROOT}/b/seg2.ts\n"


def test_relative_lines_are_left_alone():
    assert playlist.rewrite("seg3.ts\nsub/seg4.ts\n", OPTS) == "seg3.ts\nsub/seg4.ts\n"


def test_another_origin_gets_its_own_proxy_with_the_request_headers_only():
    out = playlist.rewrite("https://cdn.example/x/seg5.ts\n", OPTS)
    assert out == "/proxy/d=https%3A%2F%2Fcdn.example&h=X-Token%3Aabc/x/seg5.ts\n"


def test_uri_attributes_are_rewritten_and_the_rest_of_the_tag_kept():
    text = '#EXT-X-KEY:METHOD=AES-128,URI="/keys/k1",IV=0x1\n#EXTINF:4.0,\n'
    assert playlist.rewrite(text, OPTS) == (
        f'#EXT-X-KEY:METHOD=AES-128,URI="{ROOT}/keys/k1",IV=0x1\n#EXTINF:4.0,\n')


def test_line_endings_are_kept():
    assert playlist.rewrite("#EXTM3U\r\n/a.ts\r\n", OPTS) == f"#EXTM3U\r\n{ROOT}/a.ts\r\n"


def test_detection_by_extension_or_content_type():
    assert playlist.is_playlist("/live/index.m3u8", "")
    assert playlist.is_playlist("/live/list.M3U", "text/plain")
    assert playlist.is_playlist("/live/index", "application/vnd.apple.mpegurl")
    assert not playlist.is_playlist("/movie.mp4", "video/mp4")


def test_the_rewrite_stops_at_its_limit():
    """Many short lines each grow by the whole proxy prefix -- the limit has to hold part-way."""
    with pytest.raises(playlist.TooLarge):
        playlist.rewrite("/a\n" * 1000, OPTS, limit=10_000)
    assert playlist.rewrite("/a\n", OPTS, limit=10_000) == f"{ROOT}/a\n"


def test_lines_are_split_exactly_as_they_came():
    """No trailing newline, an empty playlist, blank lines: all kept as they came."""
    assert playlist.rewrite("", OPTS) == ""
    assert playlist.rewrite("/a", OPTS) == f"{ROOT}/a"
    assert playlist.rewrite("\n\n/a\n\n", OPTS) == f"\n\n{ROOT}/a\n\n"
