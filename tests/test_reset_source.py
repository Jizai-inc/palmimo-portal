from __future__ import annotations

import pytest

from palmimo_portal.core.reset_source import reset_source_allowed
from palmimo_portal.testing.fakes import FakeLocalAddressPort


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("127.0.0.1", False),
        ("127.42.1.2", False),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:198.51.100.11", False),
        ("198.51.100.11", False),
        ("2001:db8::11", False),
        ("fe80::11", False),
        ("fe80::11%eth0", False),
        ("198.51.100.20", True),
        ("2001:db8::20", True),
        ("::ffff:198.51.100.20", True),
        ("testclient", False),
        (None, False),
    ],
)
def test_reset_source_allows_only_remote_addresses(host: str | None, allowed: bool) -> None:
    addresses = FakeLocalAddressPort()
    addresses.addresses.update({"198.51.100.10", "198.51.100.11", "2001:db8::11", "fe80::11"})

    assert reset_source_allowed(host, addresses) is allowed


def test_reset_source_uses_current_addresses() -> None:
    addresses = FakeLocalAddressPort()
    assert reset_source_allowed("198.51.100.11", addresses)
    addresses.addresses.add("198.51.100.11")
    assert not reset_source_allowed("198.51.100.11", addresses)
    addresses.addresses.clear()
    assert reset_source_allowed("198.51.100.11", addresses)
