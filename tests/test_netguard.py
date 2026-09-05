"""Who is allowed to reach the addon: the private-network half of its two gates."""
from stremiosrv.library import netguard as ng


def test_a_forwarded_address_is_believed_only_from_our_own_proxy():
    """nginx sits in front on :12470, so the peer is loopback and the real client is in the header.
    Believing that header from a non-loopback peer would let anyone claim to be on the LAN."""
    assert ng.client_ip("127.0.0.1", "192.168.1.20") == "192.168.1.20"
    assert ng.client_ip("::1", "192.168.1.20") == "192.168.1.20"
    assert ng.client_ip("203.0.113.9", "192.168.1.20") == "203.0.113.9"


def test_the_first_hop_in_a_chain_is_the_client():
    assert ng.client_ip("127.0.0.1", "192.168.1.20, 10.0.0.1") == "192.168.1.20"


def test_a_missing_header_leaves_the_peer_as_the_client():
    assert ng.client_ip("192.168.1.20", "") == "192.168.1.20"


def test_private_ranges_are_allowed_and_public_ones_are_not():
    for ip in ("127.0.0.1", "10.1.2.3", "172.16.5.5", "192.168.1.20", "::1", "fd00::1"):
        assert ng.is_allowed(ip, ng.DEFAULT_ALLOW), ip
    for ip in ("203.0.113.9", "8.8.8.8", "2001:db8::1"):
        assert not ng.is_allowed(ip, ng.DEFAULT_ALLOW), ip


def test_carrier_grade_nat_is_allowed_because_that_is_what_a_private_tunnel_uses():
    """Private mesh-VPN tunnels commonly hand out addresses from 100.64.0.0/10 (RFC 6598,
    carrier-grade NAT), which is not RFC1918. Reaching your own box over such a tunnel is a
    legitimate way to use this and must not be refused as 'not local'."""
    assert ng.is_allowed("100.101.102.103", ng.DEFAULT_ALLOW)


def test_an_operator_can_replace_the_list_and_junk_is_ignored():
    allow = ng.parse_allow("10.9.0.0/16, not-a-cidr")
    assert allow == ("10.9.0.0/16",)
    assert ng.is_allowed("10.9.1.1", allow)
    assert not ng.is_allowed("192.168.1.20", allow)


def test_an_empty_setting_means_the_defaults():
    assert ng.parse_allow("") == ng.DEFAULT_ALLOW
    assert ng.parse_allow("   ") == ng.DEFAULT_ALLOW


def test_an_unparseable_client_address_is_refused():
    assert not ng.is_allowed("", ng.DEFAULT_ALLOW)
    assert not ng.is_allowed("not-an-ip", ng.DEFAULT_ALLOW)
