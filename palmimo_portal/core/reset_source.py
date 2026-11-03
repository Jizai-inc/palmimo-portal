from ipaddress import IPv6Address, ip_address

from palmimo_portal.ports import LocalAddressPort


def reset_source_allowed(host: str | None, addresses: LocalAddressPort) -> bool:
    """Device apps can connect through any assigned address, not just loopback."""
    if host is None:
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return False
    if isinstance(address, IPv6Address):
        address = address.ipv4_mapped or IPv6Address(str(address).split("%", 1)[0])
    return not address.is_loopback and not addresses.is_local_address(str(address))
