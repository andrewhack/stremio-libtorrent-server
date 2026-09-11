"""Where /proxy may connect: anywhere for a home client, public addresses only otherwise."""
from __future__ import annotations

import socket

import pytest

from stremiosrv.proxy import dest


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_internet_clients_may_reach_public_addresses(address):
    assert dest.allowed(address, home_client=False)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "::1", "10.1.2.3", "172.17.0.1", "192.168.5.1", "169.254.169.254",
    "100.64.0.1", "fc00::1", "fe80::1", "0.0.0.0", "224.0.0.1", "::ffff:10.0.0.1", "not-an-ip",
])
def test_internet_clients_may_not_reach_private_loopback_or_special_addresses(address):
    assert not dest.allowed(address, home_client=False)


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.5.1", "8.8.8.8"])
def test_home_clients_may_reach_anything(address):
    assert dest.allowed(address, home_client=True)


def _resolver(monkeypatch, addresses):
    monkeypatch.setattr(dest.socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
        for a in addresses])


def test_pick_skips_a_private_answer_for_an_internet_client(monkeypatch):
    _resolver(monkeypatch, ["10.0.0.5", "93.184.216.34"])
    assert dest.pick("cdn.example", 443, home_client=False) == "93.184.216.34"


def test_pick_refuses_when_every_answer_is_private(monkeypatch):
    _resolver(monkeypatch, ["10.0.0.5", "127.0.0.1"])
    with pytest.raises(dest.Refused):
        dest.pick("rebind.example", 80, home_client=False)


def test_pick_takes_the_first_answer_for_a_home_client(monkeypatch):
    _resolver(monkeypatch, ["10.0.0.5", "93.184.216.34"])
    assert dest.pick("nas.lan", 80, home_client=True) == "10.0.0.5"
