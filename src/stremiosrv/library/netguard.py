"""Is this request coming from inside the network?

Installing an addon writes its URL into the owner's Stremio *account*, which syncs to their other
devices -- that is what lets a TV inherit the install, and it is also how the URL and its token
leave the LAN and come to rest in account storage. The data behind the URL never leaves, but a
synced secret should not be the only thing between the library and the internet.
"""
from __future__ import annotations

import ipaddress

DEFAULT_ALLOW: tuple[str, ...] = (
    "127.0.0.0/8", "::1/128",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10", "fc00::/7",
    # Carrier-grade NAT (RFC 6598): private mesh-VPN tunnels hand these out, and reaching your own
    # box over one is exactly the case this guard must not break.
    "100.64.0.0/10",
)


def parse_allow(spec: str) -> tuple[str, ...]:
    """Operator-supplied CIDRs, or the defaults when unset. Unparseable entries are dropped rather
    than failing the server to start: a typo must not take the library down."""
    out = []
    for part in (spec or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            continue
        out.append(part)
    return tuple(out) if out else DEFAULT_ALLOW


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def client_ip(peer: str, forwarded_for: str) -> str:
    """The address to judge.

    `X-Forwarded-For` is believed only when the direct peer is loopback -- that is, when the request
    really did arrive through our own nginx. From any other peer the header is just something the
    caller typed.
    """
    if _is_loopback(peer or ""):
        first = (forwarded_for or "").split(",")[0].strip()
        if first:
            return first
    return peer or ""


def is_allowed(ip: str, allow: tuple[str, ...] = DEFAULT_ALLOW) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in allow:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False
