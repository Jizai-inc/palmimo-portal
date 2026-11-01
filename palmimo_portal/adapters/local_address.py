import errno
import socket
from ipaddress import IPv6Address, ip_address


class SocketLocalAddressPort:
    """Probe current address ownership; uncertainty is treated as local (fail closed)."""

    def is_local_address(self, address: str) -> bool:
        ip = ip_address(address)
        family = socket.AF_INET6 if isinstance(ip, IPv6Address) else socket.AF_INET
        try:
            # Uvicorn drops the scope ID from link-local IPv6 peer addresses.
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
                        return True
        except OSError:
            return True
        return False
