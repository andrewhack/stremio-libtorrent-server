"""`/proxy/<opts>/<path>`: the options segment, read and written.

stremio-video builds the segment with URLSearchParams (stremio-core builds the same for external
players): `d` is the destination origin, `h` a request header and `r` a response header, each
`Name:value` and repeatable -- `d=https%3A%2F%2Fcdn.example&h=User-Agent%3AFoo`. It has to be read
from the RAW path: the encoded `%2F`s are what keep the origin inside one segment, and the ASGI
server has already decoded them in the path the router sees.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

# Node's querystring.escape leaves exactly these unescaped, so rewritten playlist URLs use the same
# alphabet as the ones the stock server writes.
_SAFE = "-_.!~*'()"
_LINE_BREAKERS = ("\r", "\n", "\0")
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # an RFC 9110 field name


@dataclass(frozen=True)
class ProxyOpts:
    dest: str                                      # scheme://host[:port] -- never a path
    req_headers: tuple[tuple[str, str], ...] = ()  # `h`, in order
    res_headers: tuple[tuple[str, str], ...] = ()  # `r`, in order


def split_header(spec: str) -> tuple[str, str] | None:
    """`Name:value` -> (name, value), split at the first colon as stock does. None for anything
    http.client could not send as a header: a name that is not an RFC 9110 token, a value that is
    not Latin-1, or CR, LF or NUL anywhere (CR and LF would inject a line)."""
    name, sep, value = spec.partition(":")
    name, value = name.strip(), value.strip()
    if not sep or not _TOKEN.fullmatch(name) or any(c in spec for c in _LINE_BREAKERS):
        return None
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return None
    return name, value


def _origin(value: str) -> str | None:
    """`scheme://host[:port]` of a usable destination, or None -- also for a host or port that
    cannot be parsed, which would otherwise fail later, mid-request."""
    try:
        u = urllib.parse.urlsplit(value)  # a broken IPv6 literal raises ValueError
        port = u.port  # so does a port outside 0-65535, or one that is not a number
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname or port == 0:
        return None
    return f"{u.scheme}://{u.netloc}"


def parse(raw: str) -> tuple[ProxyOpts, str] | None:
    """The raw remainder after `/proxy/` -> (options, raw path to request, starting with `/`).

    None when there is no usable destination. Stock ignores any path inside `d` (the route's own
    path replaces it), and so does this. The returned path stays percent-encoded: it is forwarded,
    not interpreted.
    """
    segment, _, rest = raw.partition("/")
    q = urllib.parse.parse_qs(segment, keep_blank_values=True)
    dest = _origin((q.get("d") or [""])[0])
    if dest is None:
        return None
    req = tuple(h for h in map(split_header, q.get("h", [])) if h)
    res = tuple(h for h in map(split_header, q.get("r", [])) if h)
    return ProxyOpts(dest, req, res), "/" + rest


def serialize(o: ProxyOpts) -> str:
    """ProxyOpts -> the options segment, for the URLs a rewritten playlist points back at."""
    def q(value: str) -> str:
        return urllib.parse.quote(value, safe=_SAFE)

    parts = [f"d={q(o.dest)}"]
    parts.extend(f"h={q(f'{n}:{v}')}" for n, v in o.req_headers)
    parts.extend(f"r={q(f'{n}:{v}')}" for n, v in o.res_headers)
    return "&".join(parts)
