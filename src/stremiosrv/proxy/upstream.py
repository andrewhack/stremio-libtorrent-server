"""One upstream request for `/proxy`, redirects followed by hand the way the stock server does.

Stock resolves a redirect's Location against the current destination's ORIGIN, re-applies the `h`
headers, and treats a fifth redirect as an error. Every hop here goes through dest.pick and
connects to the address that passed. TLS certificates are not verified: the stock proxy does not
verify them either, and matching it was the owner's decision (2026-09-11) -- the destination rule,
not the certificate, is what keeps the proxy off the LAN.

The whole answer -- every hop's connect, TLS handshake and headers, and a playlist's body -- has
DEADLINE to arrive (owner's decision, 2026-09-13): each blocking step waits no longer than what is
left of it, and none starts once it is up. A plain per-read timeout cannot bound that: an upstream
sending a byte every few seconds never trips one, and would hold a place and a worker thread for
as long as it liked.
"""
from __future__ import annotations

import http.client
import socket
import ssl
import time
import urllib.parse
from collections.abc import Buffer, Callable

from stremiosrv.proxy import dest

MAX_REDIRECTS = 4  # stock follows four; its fifth throws "Too many redirects"
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 60.0
DEADLINE = 30.0  # for the whole answer; past it the route answers 504


class DeadlinePassed(TimeoutError):
    """The upstream did not answer within its deadline. A TimeoutError, so every handler that
    treats a slow upstream as a failed one already catches it."""


class Deadline:
    """One proxied request's time for its upstream to answer.

    Used by one thread at a time -- the route's until stop(), then the streamed body's, which only
    reads it."""

    def __init__(self, seconds: float) -> None:
        self._ends = time.monotonic() + seconds
        self._passed = False
        self._in_time: bool | None = None  # set by the first stop()

    def _budget(self, cap: float) -> float:
        if self._in_time is not None:
            return cap
        left = self._ends - time.monotonic()
        if left <= 0:
            self._passed = True
            raise DeadlinePassed
        return min(cap, left)

    def check(self) -> None:
        """Raise DeadlinePassed when the time is up (never after stop())."""
        self._budget(0.0)

    def run[T](self, sock: socket.socket, cap: float, step: Callable[..., T], *args: object) -> T:
        """One blocking step on `sock` -- a connect, a handshake, a read, a send -- with its own
        timeout `cap`, or what is left of the deadline when that is less. A step the deadline cut
        short raises DeadlinePassed; one that ran out of its own timeout, a plain TimeoutError."""
        timeout = self._budget(cap)
        if sock.gettimeout() != timeout:
            sock.settimeout(timeout)
        try:
            return step(*args)
        except TimeoutError:
            if timeout < cap:
                self._passed = True
                raise DeadlinePassed from None
            raise

    def stop(self) -> bool:
        """End the deadline -- before a streamed body, which may rightly run for hours on the plain
        per-read timeout. True when it did not pass; asking again gives the same answer."""
        if self._in_time is None:
            self._in_time = not self._passed
        return self._in_time


class _Budgeted(socket.socket):
    """A plain socket whose reads and sends go through its request's deadline. http.client reads
    a response only through recv_into, so headers, a playlist and a streamed body all pass here."""

    deadline: Deadline

    def recv_into(self, buffer: Buffer, nbytes: int = 0, flags: int = 0) -> int:
        return self.deadline.run(self, READ_TIMEOUT, super().recv_into, buffer, nbytes, flags)

    def sendall(self, data: Buffer, flags: int = 0) -> None:
        self.deadline.run(self, READ_TIMEOUT, super().sendall, data, flags)


class _BudgetedTLS(ssl.SSLSocket):
    """The same over TLS; _PinnedTLS runs the handshake through the deadline itself."""

    deadline: Deadline

    def recv_into(self, buffer: Buffer, nbytes: int | None = None, flags: int = 0) -> int:
        return self.deadline.run(self, READ_TIMEOUT, super().recv_into, buffer, nbytes, flags)

    def sendall(self, data: Buffer, flags: int = 0) -> None:
        self.deadline.run(self, READ_TIMEOUT, super().sendall, data, flags)


# One context for every hop: it verifies nothing (see above), so nothing in it depends on the
# destination -- and an HLS stream opens a connection per segment.
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE
_TLS.sslsocket_class = _BudgetedTLS


class TooManyRedirects(Exception):
    """A fifth redirect in a row, as the stock proxy counts them."""


class BadUpstream(Exception):
    """A URL that cannot be requested: a malformed destination, port or host name, a header
    http.client cannot send, or a redirect to any of those."""


class _Pinned(http.client.HTTPConnection):
    """Connect to an address already checked, while speaking to the host by name -- every step
    within the request's deadline."""

    def __init__(self, address: str, host: str, port: int, deadline: Deadline) -> None:
        super().__init__(host, port, timeout=READ_TIMEOUT)
        self._address = address
        self._deadline = deadline

    def connect(self) -> None:
        family = socket.AF_INET6 if ":" in self._address else socket.AF_INET
        sock = _Budgeted(family, socket.SOCK_STREAM)
        sock.deadline = self._deadline
        try:
            self._deadline.run(sock, CONNECT_TIMEOUT, sock.connect, (self._address, self.port))
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class _PinnedTLS(_Pinned):
    def connect(self) -> None:
        super().connect()
        # Hand-shaken here rather than inside wrap_socket, so the handshake gets its budget too.
        tls = _TLS.wrap_socket(self.sock, server_hostname=self.host,
                               do_handshake_on_connect=False)
        tls.deadline = self._deadline
        self.sock = tls
        self._deadline.run(tls, READ_TIMEOUT, tls.do_handshake)


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


def open_url(url: str, method: str, headers: dict[str, str], home_client: bool,
             deadline: Deadline) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
    """(response, connection) for `url` after any redirects; the caller closes both, and stops
    `deadline` once it has what it needs from the answer.

    Raises dest.Refused for a destination this client may not reach (or a redirect to anything but
    http/https), BadUpstream for a URL that cannot be requested, TooManyRedirects, DeadlinePassed
    when the time runs out, and OSError / http.client.HTTPException when the upstream cannot be
    reached or spoken to."""
    for _ in range(MAX_REDIRECTS + 1):
        deadline.check()  # no hop starts -- not even its name lookup -- once the time is up
        try:
            u = urllib.parse.urlsplit(url)
            port = u.port or (443 if u.scheme == "https" else 80)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise dest.Refused(u.scheme)
            address = dest.pick(u.hostname, port, home_client)
        except ValueError:  # a malformed URL, port or host name (dest.Refused is not a ValueError)
            raise BadUpstream from None
        conn = (_PinnedTLS if u.scheme == "https" else _Pinned)(address, u.hostname, port, deadline)
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
