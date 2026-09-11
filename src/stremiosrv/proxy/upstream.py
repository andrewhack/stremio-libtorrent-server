"""One upstream request for `/proxy`, redirects followed by hand the way the stock server does.

Stock resolves a redirect's Location against the current destination's ORIGIN, re-applies the `h`
headers, and treats a fifth redirect as an error. Every hop here goes through dest.pick and
connects to the address that passed. TLS certificates are not verified: the stock proxy does not
verify them either, and matching it was the owner's decision (2026-09-11) -- the destination rule,
not the certificate, is what keeps the proxy off the LAN.
"""
from __future__ import annotations

import http.client
import socket
import ssl
import urllib.parse

from stremiosrv.proxy import dest

MAX_REDIRECTS = 4  # stock follows four; its fifth throws "Too many redirects"
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 60.0

# One context for every hop: it verifies nothing (see above), so nothing in it depends on the
# destination -- and an HLS stream opens a connection per segment.
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


class TooManyRedirects(Exception):
    """A fifth redirect in a row, as the stock proxy counts them."""


class BadUpstream(Exception):
    """A URL that cannot be requested: a malformed destination, port or host name, a header
    http.client cannot send, or a redirect to any of those."""


class _Pinned(http.client.HTTPConnection):
    """Connect to an address already checked, while speaking to the host by name."""

    def __init__(self, address: str, host: str, port: int) -> None:
        super().__init__(host, port, timeout=READ_TIMEOUT)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), CONNECT_TIMEOUT)
        self.sock.settimeout(READ_TIMEOUT)


class _PinnedTLS(_Pinned):
    def connect(self) -> None:
        super().connect()
        self.sock = _TLS.wrap_socket(self.sock, server_hostname=self.host)


def _host_header(u: urllib.parse.SplitResult) -> str:
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return host if u.port is None else f"{host}:{u.port}"


def _hop_headers(headers: dict[str, str], u: urllib.parse.SplitResult) -> dict[str, str]:
    """This hop's headers. A Host among the addon's `h` headers wins on every hop, as in stock;
    otherwise the hop's own. Matched in any case, so the upstream never gets two."""
    given = [v for k, v in headers.items() if k.lower() == "host"]
    out = {k: v for k, v in headers.items() if k.lower() != "host"}
    out["Host"] = given[-1] if given else _host_header(u)
    return out


def open_url(url: str, method: str, headers: dict[str, str],
             home_client: bool) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
    """(response, connection) for `url` after any redirects; the caller closes both.

    Raises dest.Refused for a destination this client may not reach (or a redirect to anything but
    http/https), BadUpstream for a URL that cannot be requested, TooManyRedirects, and OSError /
    http.client.HTTPException when the upstream cannot be reached or spoken to."""
    for _ in range(MAX_REDIRECTS + 1):
        try:
            u = urllib.parse.urlsplit(url)
            port = u.port or (443 if u.scheme == "https" else 80)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise dest.Refused(u.scheme)
            address = dest.pick(u.hostname, port, home_client)
        except ValueError:  # a malformed URL, port or host name (dest.Refused is not a ValueError)
            raise BadUpstream from None
        conn = (_PinnedTLS if u.scheme == "https" else _Pinned)(address, u.hostname, port)
        target = (u.path or "/") + (f"?{u.query}" if u.query else "")
        try:
            conn.request(method, target, headers=_hop_headers(headers, u))
            resp = conn.getresponse()
        except ValueError:  # a header http.client cannot put on the wire
            conn.close()
            raise BadUpstream from None
        except BaseException:
            conn.close()
            raise
        location = resp.getheader("location")
        if not (300 <= resp.status < 400 and location):
            return resp, conn
        resp.close()
        conn.close()
        try:
            url = urllib.parse.urljoin(f"{u.scheme}://{_host_header(u)}/", location)
        except ValueError:  # a Location that is not a URL, e.g. a broken IPv6 literal
            raise BadUpstream from None
    raise TooManyRedirects
