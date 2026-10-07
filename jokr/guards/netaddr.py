"""Is a DNS answer safe to connect to?

`ipaddress.is_global` alone is not enough: it calls NAT64, IPv4-compatible and
site-local IPv6 addresses global, and its answers have changed between Python
patch releases. So we deny the special ranges explicitly and, for IPv6 forms
that carry an IPv4 address inside, require that inner address to be public too.
"""

import ipaddress

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_COMPAT = ipaddress.IPv6Network("::/96")
_IPV4_TRANSLATED = ipaddress.IPv6Network("::ffff:0:0:0/96")
_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")


def _v4_public(ip: ipaddress.IPv4Address) -> bool:
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _embedded_v4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address an IPv6 address may route to."""
    inner: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        inner.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        inner.append(ip.sixtofour)
    if ip.teredo is not None:
        inner.extend(ip.teredo)
    if any(ip in net for net in (_NAT64, _IPV4_COMPAT, _IPV4_TRANSLATED)):
        inner.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return inner


def is_public_address(address: str) -> bool:
    """True only for an address that is plainly on the public internet."""
    try:
        # Drop an IPv6 zone id ("fe80::1%eth0"); a zoned address is link-local anyway.
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return _v4_public(ip)
    embedded = _embedded_v4(ip)
    if embedded:
        return all(_v4_public(v4) for v4 in embedded) and ip not in _IPV4_COMPAT
    if ip in _NAT64_LOCAL or ip.is_site_local or ip.is_multicast:
        return False
    return ip in _GLOBAL_UNICAST and ip.is_global and not ip.is_private
