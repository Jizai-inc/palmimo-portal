from __future__ import annotations

import errno
import socket
from types import SimpleNamespace, TracebackType

import pytest

from palmimo_portal.adapters.local_address import SocketLocalAddressPort


@pytest.fixture
def socket_backend(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    backend = SimpleNamespace(
        AF_INET=socket.AF_INET,
        AF_INET6=socket.AF_INET6,
        SOCK_DGRAM=socket.SOCK_DGRAM,
        socket=socket.socket,
        if_nameindex=lambda: [(2, "eth0"), (7, "wlan0")],
    )
    monkeypatch.setattr("palmimo_portal.adapters.local_address.socket", backend)
    return backend


def test_socket_address_probe_distinguishes_local_and_remote_addresses() -> None:
    adapter = SocketLocalAddressPort()
    assert adapter.is_local_address("127.0.0.1")
    assert not adapter.is_local_address("192.0.2.1")
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback is unavailable")
    assert adapter.is_local_address("::1")
    assert not adapter.is_local_address("2001:db8::1")


@pytest.mark.parametrize(
    "host", ["198.51.100.11", "2001:db8::11", "fe80::11", "198.51.100.20", "2001:db8::20", "fe80::20"]
)
def test_socket_address_probe_finds_assigned_addresses_including_scopeless_link_local(
    socket_backend: SimpleNamespace, host: str
) -> None:
    class FakeSocket:
        def __init__(self, family: int, kind: int) -> None:
            pass

        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(
            self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
        ) -> None:
            pass

        def bind(self, address: tuple[str, int] | tuple[str, int, int, int]) -> None:
            scope = address[3] if len(address) == 4 else 0
            if (address[0], scope) in {("198.51.100.11", 0), ("2001:db8::11", 0), ("fe80::11", 7)}:
                return
            raise OSError(errno.EADDRNOTAVAIL, "not assigned")

    socket_backend.socket = FakeSocket
    assert SocketLocalAddressPort().is_local_address(host) is host.endswith("11")


@pytest.mark.parametrize("operation", ["socket", "if_nameindex"])
def test_socket_address_probe_fails_closed_on_os_errors(socket_backend: SimpleNamespace, operation: str) -> None:
    def fail(*args: object) -> None:
        raise OSError(errno.EACCES, "denied")

    setattr(socket_backend, operation, fail)
    assert SocketLocalAddressPort().is_local_address("fe80::11")
