"""DNS answers: only plainly public addresses pass."""

import pytest

from jokr.guards.netaddr import is_public_address


@pytest.mark.parametrize("address", ["151.101.1.1", "8.8.8.8", "2a04:4e42::81", "2606:4700::1111"])
def test_public_addresses_pass(address: str) -> None:
    assert is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104 - a test input, not a bind address
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fd00::1",  # unique local
        "fec0::1",  # site-local
        "ff02::1",  # multicast
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:0:7f00:1",  # IPv4-translated
        "::7f00:1",  # IPv4-compatible
        "64:ff9b::7f00:1",  # NAT64 -> 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 -> 169.254.169.254
        "64:ff9b:1::1",  # local-use NAT64
        "2002:7f00:1::1",  # 6to4 -> 127.0.0.1
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "2001:db8::1",  # documentation
        "not-an-ip",
    ],
)
def test_internal_or_special_addresses_fail(address: str) -> None:
    assert not is_public_address(address)


def test_nat64_of_a_public_address_passes() -> None:
    assert is_public_address("64:ff9b::808:808")  # 8.8.8.8
