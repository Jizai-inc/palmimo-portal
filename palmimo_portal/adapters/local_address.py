import errno
import logging
import socket
from ipaddress import IPv6Address, ip_address


logger = logging.getLogger("palmimo_portal")


class SocketLocalAddressPort:
    """Probe current address ownership; uncertainty is treated as local (fail closed).

    HostGuard's cached interface enumeration sees only primary IPv4 addresses,
    excluding secondary IPv4 and IPv6; missing an address here would allow resets.
    With net.ipv4.ip_nonlocal_bind=1 or equivalent, arbitrary addresses can bind,
    so all peers are classified as local and resets are refused.
    """

    def is_local_address(self, address: str) -> bool:
        ip = ip_address(address)
        family = socket.AF_INET6 if isinstance(ip, IPv6Address) else socket.AF_INET
        try:
            # If Portal listens on IPv6, uvicorn omits link-local peer scope IDs.
            scopes = [index for index, _ in socket.if_nameindex()] if ip.version == 6 and ip.is_link_local else [0]
            for scope in scopes:
                try:
                    with socket.socket(family, socket.SOCK_DGRAM) as probe:
                        if family == socket.AF_INET6:
                            probe.bind((address, 0, 0, scope))
                        else:
                            probe.bind((address, 0))
                    return True
                except OSError as error:
                    if error.errno not in {errno.EADDRNOTAVAIL, errno.ENODEV}:
                        logger.warning("auth: local address probe failed for %s (errno %s)", address, error.errno)
                        return True
        except OSError as error:
            logger.warning("auth: local address probe failed for %s (errno %s)", address, error.errno)
            return True
        return False
