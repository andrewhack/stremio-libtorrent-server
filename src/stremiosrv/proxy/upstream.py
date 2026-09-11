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


class TooManyRedirects(Exception):
    """A fifth redirect in a row, as the stock proxy counts them."""


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
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)


def _host_header(u: urllib.parse.SplitResult) -> str:
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return host if u.port is None else f"{host}:{u.port}"


def open_url(url: str, method: str, headers: dict[str, str], home_client: bool):
    """(response, connection) for `url` after any redirects; the caller closes both.

    `headers` must not carry Host -- each hop sets its own. Raises dest.Refused for a destination
    this client may not reach (or a redirect to anything but http/https), TooManyRedirects, and
    OSError / http.client.HTTPException when the upstream cannot be reached or spoken to."""
    for _ in range(MAX_REDIRECTS + 1):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise dest.Refused(u.scheme)
        port = u.port or (443 if u.scheme == "https" else 80)
        address = dest.pick(u.hostname, port, home_client)
        conn = (_PinnedTLS if u.scheme == "https" else _Pinned)(address, u.hostname, port)
        target = (u.path or "/") + (f"?{u.query}" if u.query else "")
        try:
            conn.request(method, target, headers={**headers, "Host": _host_header(u)})
            resp = conn.getresponse()
        except BaseException:
            conn.close()
            raise
        location = resp.getheader("location")
        if not (300 <= resp.status < 400 and location):
            return resp, conn
        resp.close()
        conn.close()
        url = urllib.parse.urljoin(f"{u.scheme}://{_host_header(u)}/", location)
    raise TooManyRedirects
