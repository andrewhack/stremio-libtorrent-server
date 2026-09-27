import types

import pytest
from fastapi import HTTPException

from stremiosrv.api import media_fetch


def _req(server_url="https://box.example:12470", host="box.example:12470", peer="127.0.0.1",
         allow="127.0.0.0/8,192.168.0.0/16", origin=None, port=11470):
    st = types.SimpleNamespace(server_url=server_url, library_addon_allow=allow, http_port=port)
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=st))
    headers = {"host": host}
    if origin:
        headers["origin"] = origin
    return types.SimpleNamespace(headers=headers, client=types.SimpleNamespace(host=peer), app=app)


def test_register_round_trips_and_is_unguessable():
    media_fetch.reset()
    t = media_fetch.register("https://cdn.example/v.mkv", True)
    assert len(t) >= 16 and media_fetch.resolve_ticket(t) == ("https://cdn.example/v.mkv", True)
    assert media_fetch.resolve_ticket("nope") is None


def test_registry_is_bounded_but_keeps_recently_used():
    media_fetch.reset()
    first = media_fetch.register("https://cdn.example/0", True)
    oldest_untouched = media_fetch.register("https://cdn.example/1", True)
    for i in range(2, media_fetch.TICKET_CAP):
        media_fetch.register(f"https://cdn.example/{i}", True)
    # registry is now exactly at TICKET_CAP; refresh `first` so it is no longer the LRU entry --
    # a plain FIFO cap (no refresh-on-read) would pass the old version of this test too, since
    # `first` was also the very first entry inserted. This makes the refresh load-bearing.
    media_fetch.resolve_ticket(first)
    for i in range(media_fetch.TICKET_CAP, media_fetch.TICKET_CAP + 5):
        media_fetch.register(f"https://cdn.example/{i}", True)
    assert media_fetch.resolve_ticket(first) == ("https://cdn.example/0", True)  # survived
    assert media_fetch.resolve_ticket(oldest_untouched) is None  # evicted instead: never refreshed


def test_our_own_url_passes_through():
    r = _req()
    for url in ("https://box.example:12470/aabb/0",
                "http://127.0.0.1:11470/aabb/0",
                "https://box.example:12470/proxy/d=x/seg.ts"):
        assert media_fetch.resolve_media_input(r, url) == url


def test_resolve_own_host_other_port_is_passthrough():
    # our own name, a different port: still our box, not the LAN -- passed through unchanged
    r = _req()
    url = "http://box.example:9999/aabb/0"
    assert media_fetch.resolve_media_input(r, url) == url


def _stub_public_dns(monkeypatch):
    """cdn.example/evil.example are RFC 2606's reserved, deliberately non-resolving pseudo-TLD --
    same as test_proxy_dest.py's illustrative hostnames -- so resolve_media_input's dest.pick check
    needs a stubbed resolver here too, or these two tests would depend on real DNS for a name that
    can never resolve."""
    from stremiosrv.proxy import dest
    monkeypatch.setattr(dest.socket, "getaddrinfo", lambda host, port, **kw: [
        (dest.socket.AF_INET, dest.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
    ])


def test_external_url_becomes_a_reader_url(monkeypatch):
    media_fetch.reset()
    _stub_public_dns(monkeypatch)
    r = _req()
    out = media_fetch.resolve_media_input(r, "https://cdn.example/v.mkv")
    assert out.startswith("http://127.0.0.1:11470" + media_fetch.READER_PREFIX + "/")


def test_resolve_userinfo_host_is_not_ours(monkeypatch):
    media_fetch.reset()
    _stub_public_dns(monkeypatch)
    r = _req()
    out = media_fetch.resolve_media_input(r, "http://box.example@evil.example/v.mkv")
    assert out.startswith("http://127.0.0.1:11470" + media_fetch.READER_PREFIX + "/")


def test_refused_destination_raises_403():
    r = _req(peer="8.8.8.8")  # internet client
    with pytest.raises(HTTPException) as ei:
        media_fetch.resolve_media_input(r, "http://192.168.1.10/v.mkv")  # LAN, refused to internet
    assert ei.value.status_code == 403


def test_non_http_raises_403():
    r = _req()
    with pytest.raises(HTTPException) as ei:
        media_fetch.resolve_media_input(r, "file:///etc/hostname")
    assert ei.value.status_code == 403


def test_spoofed_host_header_does_not_pass_an_external_url_through():
    # nginx forwards Host verbatim (no server_name / TrustedHostMiddleware) -- an internet caller
    # can set Host to match the mediaURL's host, hoping is_own_media_url treats "matches Host" as
    # "ours" and returns the URL unchanged, skipping the guard entirely. It must still be
    # dest-checked: here that means a LAN address refused to an internet-classified caller.
    r = _req(host="192.168.1.10", peer="8.8.8.8")
    with pytest.raises(HTTPException) as ei:
        media_fetch.resolve_media_input(r, "http://192.168.1.10/v.mkv")
    assert ei.value.status_code == 403


def test_malformed_url_is_403():
    r = _req()
    with pytest.raises(HTTPException) as ei:
        media_fetch.resolve_media_input(r, "http://[::1:80/evil")
    assert ei.value.status_code == 403
