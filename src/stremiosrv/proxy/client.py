"""Who counts as a home client -- the one decision /proxy and the media reader share.

A client on the home network (STREMIOSRV_LIBRARY_ADDON_ALLOW, the rule the library addon applies)
gets the home rule; a web page on another site than this server or the Stremio web app is judged
like an internet client, because CORS is open and any site a home viewer opens could otherwise read
the LAN through the viewer's own server (owner's decision, 2026-09-12)."""
from __future__ import annotations

import urllib.parse

from fastapi import Request

from stremiosrv.library import netguard

# Web origins of the official Stremio web app. A page on another site -- or one that hides its
# origin ("null") -- is judged like an internet client.
STREMIO_WEB_ORIGINS = frozenset({"https://web.stremio.com", "https://app.strem.io"})


def host_of(url: str) -> str:
    """The host a URL names, lowercase; "" when it names none or cannot be parsed."""
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def is_foreign_page(request: Request) -> bool:
    """Whether a web page on another site than this server or the Stremio web app sent this."""
    origin = request.headers.get("origin")
    if origin is None or origin in STREMIO_WEB_ORIGINS:
        return False
    try:
        u = urllib.parse.urlsplit(origin)
        host = u.hostname or ""
    except ValueError:
        return True
    if u.scheme not in ("http", "https") or not host:
        return True
    settings = request.app.state.settings
    own = {host_of("//" + request.headers.get("host", "")), host_of(settings.server_url)}
    if host in own:
        return False
    return not netguard.is_allowed(host, netguard.parse_allow(settings.library_addon_allow))


def is_home_client(request: Request) -> bool:
    """Whether this request gets the home rule: a client on the home network, and not a web page on
    another site."""
    if is_foreign_page(request):
        return False
    peer = request.client.host if request.client else ""
    ip = netguard.client_ip(peer, request.headers.get("x-forwarded-for", ""))
    allow = netguard.parse_allow(request.app.state.settings.library_addon_allow)
    return netguard.is_allowed(ip, allow)
