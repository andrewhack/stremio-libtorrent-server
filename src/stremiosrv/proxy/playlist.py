"""Rewrite an HLS playlist fetched through `/proxy`, so every URL in it comes back through it too.

The stock rules: an absolute URL on the destination's own origin, and a root-relative line, join
the same `/proxy/<opts>` the playlist came through; an absolute URL on another origin gets a
`/proxy/` of its own (that origin, the same request headers, no forced response headers); a
relative line is left alone, because the player resolves it against the playlist's own URL, which
is already a proxy URL. In a tag line only the first `URI="..."` attribute is rewritten.

The playlist stays bytes and each line is decoded on its own, so what a rewrite holds follows its
size in bytes: a whole playlist decoded at once is stored four bytes to a character as soon as one
character in it lies outside the BMP.
"""
from __future__ import annotations

import re
import urllib.parse
from collections.abc import Iterator

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


def _rewrite_line(line: str, root: str, o: ProxyOpts) -> str:
    """One line without its line ending: a tag's first URI attribute, or a URL line."""
    if line.startswith("#"):
        m = _URI_ATTR.search(line)
        if not m:
            return line
        return line[:m.start(1)] + _rewrite_url(m.group(1), root, o) + line[m.end(1):]
    return _rewrite_url(line, root, o) if line else line


class TooLarge(Exception):
    """The rewritten playlist would pass the caller's limit."""


def _lines(body: bytes) -> Iterator[bytes]:
    """body.split(b"\n"), one line at a time, so a playlist of many short lines never becomes a
    list of as many objects."""
    start = 0
    while (end := body.find(b"\n", start)) >= 0:
        yield body[start:end]
        start = end + 1
    yield body[start:]


def rewrite(body: bytes, o: ProxyOpts, limit: int | None = None) -> bytes:
    """The playlist with its URLs routed back through /proxy; line endings, and bytes that are not
    UTF-8, kept as they came.

    Raises TooLarge as soon as the output would pass `limit` bytes: every rewritten line grows by
    the whole proxy prefix, so a small playlist of many short lines can grow a great deal. Raises
    ValueError for a URL that cannot be parsed or written back."""
    root = "/proxy/" + serialize(o)
    out = bytearray()
    for i, raw in enumerate(_lines(body)):
        line = raw.decode("utf-8", "surrogateescape")
        text = line.rstrip("\r")
        piece = ("\n" if i else "") + _rewrite_line(text, root, o) + line[len(text):]
        encoded = piece.encode("utf-8", "surrogateescape")
        if limit is not None and len(out) + len(encoded) > limit:
            raise TooLarge
        out += encoded
    return bytes(out)
