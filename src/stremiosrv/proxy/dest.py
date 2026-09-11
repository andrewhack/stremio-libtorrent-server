"""Where `/proxy` may connect -- decided on the address it would actually reach.

A client on the home network may proxy to any address: it can reach them itself anyway. A client
from the internet may proxy only to globally routable addresses, so nobody outside can use the box
to reach the LAN, the Docker network, or the server's own origin-only routes on loopback. The check
runs on the RESOLVED address, for the first hop and for every redirect, and the connection goes to
the address that passed, so a DNS answer cannot change between the check and the connect.
"""
from __future__ import annotations

import ipaddress
import socket


class Refused(Exception):
    """The destination is not one this client may reach through the proxy."""


def allowed(address: str, home_client: bool) -> bool:
    if home_client:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def pick(host: str, port: int, home_client: bool) -> str:
    """The first resolved address this client may reach.

    Raises Refused when no answer is allowed; lets socket.gaierror through when the name does not
    resolve at all (the route reports that as the upstream being unreachable)."""
    for *_, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if allowed(sockaddr[0], home_client):
            return sockaddr[0]
    raise Refused(host)
