import types

from stremiosrv.proxy import client


def _req(headers=None, peer="127.0.0.1", server_url="https://box.example:12470",
         allow="127.0.0.0/8,192.168.0.0/16"):
    st = types.SimpleNamespace(server_url=server_url, library_addon_allow=allow)
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=st))
    return types.SimpleNamespace(
        headers={k.lower(): v for k, v in (headers or {}).items()},
        client=types.SimpleNamespace(host=peer),
        app=app,
    )


def test_host_of():
    assert client.host_of("https://Example.COM:8080/x") == "example.com"
    assert client.host_of("not a url") == ""


def test_loopback_peer_no_origin_is_home():
    assert client.is_home_client(_req()) is True


def test_internet_peer_is_not_home():
    assert client.is_home_client(_req(peer="8.8.8.8")) is False


def test_foreign_page_origin_is_not_home():
    # a LAN peer, but the request carries the Origin of some other website
    r = _req(peer="192.168.1.9", headers={"origin": "https://evil.example"})
    assert client.is_home_client(r) is False


def test_stremio_web_origin_stays_home():
    r = _req(peer="192.168.1.9", headers={"origin": "https://web.stremio.com"})
    assert client.is_home_client(r) is True
