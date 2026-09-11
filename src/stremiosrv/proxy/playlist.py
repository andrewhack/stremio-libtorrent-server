"""Rewrite an HLS playlist fetched through `/proxy`, so every URL in it comes back through it too.

The stock rules: an absolute URL on the destination's own origin, and a root-relative line, join
the same `/proxy/<opts>` the playlist came through; an absolute URL on another origin gets a
`/proxy/` of its own (that origin, the same request headers, no forced response headers); a
relative line is left alone, because the player resolves it against the playlist's own URL, which
is already a proxy URL. In a tag line only the first `URI="..."` attribute is rewritten.
"""
from __future__ import annotations

import re
import urllib.parse

from stremiosrv.proxy.opts import ProxyOpts, serialize

PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")
_URI_ATTR = re.compile(r'URI="([^"]+)"')


def is_playlist(path: str, content_type: str) -> bool:
    """By the requested path's extension, or by an mpegurl content type -- stock's two tests."""
    by_extension = urllib.parse.urlsplit(path).path.lower().endswith(PLAYLIST_EXTENSIONS)
    return by_extension or "mpegurl" in (content_type or "").lower()


def _join(root: str, path: str) -> str:
    return root.rstrip("/") + "/" + path.lstrip("/")


def _rewrite_url(url: str, root: str, o: ProxyOpts) -> str:
    if url.startswith(("http://", "https://")):
        u = urllib.parse.urlsplit(url)
        tail = u.path + (f"?{u.query}" if u.query else "")
        origin = f"{u.scheme}://{u.netloc}"
        if origin.lower() == o.dest.lower():
            return _join(root, tail)
        return _join("/proxy/" + serialize(ProxyOpts(origin, o.req_headers)), tail)
    if url.startswith("/"):
        return _join(root, url)
    return url


def rewrite(text: str, o: ProxyOpts) -> str:
    """The playlist with its URLs routed back through /proxy; line endings kept as they came."""
    root = "/proxy/" + serialize(o)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        body = line.rstrip("\r")
        cr = line[len(body):]
        if body.startswith("#"):
            m = _URI_ATTR.search(body)
            if m:
                body = body[:m.start(1)] + _rewrite_url(m.group(1), root, o) + body[m.end(1):]
        elif body:
            body = _rewrite_url(body, root, o)
        lines[i] = body + cr
    return "\n".join(lines)
