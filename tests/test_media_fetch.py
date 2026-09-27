import types

import pytest
from fastapi import HTTPException

from stremiosrv.api import media_fetch
from stremiosrv.proxy import opts as proxy_opts

# A real 40-hex-char infohash shape (test_library_download_api.py uses the same literal) -- the
# torrent-stream passthrough regex requires the full 40 characters, unlike the old "aabb" shorthand.
IH = "aabbccddeeff00112233445566778899aabbccdd"


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
    assert len(t) >= 16
    assert media_fetch.resolve_ticket(t) == ("https://cdn.example/v.mkv", True, ())
    assert media_fetch.resolve_ticket("nope") is None


def test_register_carries_optional_request_headers():
    media_fetch.reset()
    t = media_fetch.register("https://cdn.example/v.mkv", True, (("Authorization", "tok"),))
    assert media_fetch.resolve_ticket(t) == (
        "https://cdn.example/v.mkv", True, (("Authorization", "tok"),))


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
    assert media_fetch.resolve_ticket(first) == ("https://cdn.example/0", True, ())  # survived
    assert media_fetch.resolve_ticket(oldest_untouched) is None  # evicted instead: never refreshed


def test_our_own_url_passes_through(monkeypatch):
    # The torrent-stream shape -- the only thing ffmpeg may ever be handed directly -- still passes
    # through unchanged, at our loopback address or the SERVER_URL host, any port.
    r = _req()
    for url in (f"https://box.example:12470/{IH}/0",
                f"http://127.0.0.1:11470/{IH}/-1"):
        assert media_fetch.resolve_media_input(r, url) == url

    # C1: an own-host /proxy/... mediaURL must NEVER pass through unchanged. ffmpeg handed that URL
    # would re-enter /proxy over loopback, where is_home_client sees only the loopback caller and
    # classifies it HOME -- letting an internet client's own mediaURL escalate to a home-classified
    # fetch. It must become a reader URL instead (still fetching the same destination, but
    # dest-checked for the ORIGINAL client -- see the two tests below).
    media_fetch.reset()
    _stub_public_dns(monkeypatch)
    proxy_url = "https://box.example:12470/proxy/d=https%3A%2F%2Fcdn.example/seg.ts"
    out = media_fetch.resolve_media_input(r, proxy_url)
    assert out != proxy_url
    assert out.startswith("http://127.0.0.1:11470" + media_fetch.READER_PREFIX + "/")


def test_resolve_own_host_other_port_is_passthrough():
    # our own name, a different port: still our box, not the LAN -- passed through unchanged
    r = _req()
    url = f"http://box.example:9999/{IH}/0"
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


def test_internet_client_cannot_reenter_proxy_to_reach_lan():
    # C1: an internet client cannot use its own mediaURL to make the loopback reader re-enter
    # /proxy with a LAN destination. The /proxy URL's INNER destination (192.168.1.10, built the
    # way a real client would -- percent-encoded `d=`, via opts.serialize) is dest-checked for the
    # ORIGINAL client, an internet peer, so it is refused exactly as a direct /proxy call would be.
    # A literal IP needs no DNS stub (getaddrinfo resolves it locally, like test_proxy_dest.py).
    media_fetch.reset()
    r = _req(peer="8.8.8.8")  # internet client
    segment = proxy_opts.serialize(proxy_opts.ProxyOpts(dest="http://192.168.1.10"))
    url = f"http://127.0.0.1:11470/proxy/{segment}/seg.ts"
    with pytest.raises(HTTPException) as ei:
        media_fetch.resolve_media_input(r, url)
    assert ei.value.status_code == 403


def test_home_client_proxy_mediaurl_routes_inner_dest_through_reader(monkeypatch):
    # C1's legitimate path: a home client's own /proxy/... mediaURL (built the way a real client
    # would, with `d=` and `h=` percent-encoded via opts.serialize) still reaches its destination --
    # through the reader, keyed on the INNER url, carrying the proxy's own request headers so an
    # authenticated CDN/debrid stream still works.
    media_fetch.reset()
    _stub_public_dns(monkeypatch)
    r = _req()  # home client (peer 127.0.0.1)
    segment = proxy_opts.serialize(
        proxy_opts.ProxyOpts("https://cdn.example", (("Authorization", "tok"),)))
    url = f"https://box.example:12470/proxy/{segment}/v.mkv"
    out = media_fetch.resolve_media_input(r, url)
    assert out.startswith("http://127.0.0.1:11470" + media_fetch.READER_PREFIX + "/")
    ticket = out.rsplit("/", 1)[-1]
    assert media_fetch.resolve_ticket(ticket) == (
        "https://cdn.example/v.mkv", True, (("Authorization", "tok"),))


def test_internet_client_cannot_reenter_via_a_subtitle_subpath():
    # Residual C1 (re-review): the torrent-stream regex's old `(/.*)?` trailing-subpath allowance
    # also matched /<ih>/<idx>/subtitles.json and .../subtitles.vtt -- routes that themselves take a
    # `mediaURL` query param and re-invoke resolve_media_input (subs.py). An internet client's own
    # mediaURL pointing at one of those, carrying a nested mediaURL query string, would pass through
    # unchanged here; ffprobe/ffmpeg then opens it over loopback, and the INNER subtitles route sees
    # a LOOPBACK (home) request -- dest-checking the nested LAN target as home. Same internet-to-home
    # escalation as the /proxy door, through a different one. The legitimate own shape carries no
    # subpath (playback.py's stream routes are exactly /<ih>/<idx>), so the outer URL here must not
    # pass through unchanged: it falls through to a plain dest-check on the outer (loopback) host,
    # refused for an internet-classified caller.
    media_fetch.reset()
    r = _req(peer="8.8.8.8")  # internet client
    url = f"http://127.0.0.1:11470/{IH}/0/subtitles.json?mediaURL=http://192.168.1.10/x"
    with pytest.raises(HTTPException) as ei:
        media_fetch.resolve_media_input(r, url)
    assert ei.value.status_code == 403


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
