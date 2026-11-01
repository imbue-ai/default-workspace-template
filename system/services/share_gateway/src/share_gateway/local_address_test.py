import ipaddress

from share_gateway.local_address import primary_address


def test_primary_address_is_a_routable_ipv4_address_or_none() -> None:
    address = primary_address()
    if address is None:
        return
    parsed = ipaddress.ip_address(address)
    assert parsed.version == 4
    assert not parsed.is_loopback
    assert not parsed.is_unspecified
