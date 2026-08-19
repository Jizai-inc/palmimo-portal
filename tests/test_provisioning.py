"""Unit tests for the unprovisioned-state rule."""

from __future__ import annotations

import pytest

from palmimo_portal.core.provisioning import NotProvisionedError, is_provisioned, require_provisioned
from palmimo_portal.ports import ConnectionState, WifiStatus
from palmimo_portal.testing.fakes import FakeNetworkPort


def test_is_provisioned_false_when_no_connection_and_no_known_networks() -> None:
    network = FakeNetworkPort()

    assert is_provisioned(network) is False


def test_is_provisioned_true_when_connected() -> None:
    network = FakeNetworkPort(status=WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="10.0.0.5"))

    assert is_provisioned(network) is True


def test_is_provisioned_true_when_connecting() -> None:
    network = FakeNetworkPort(status=WifiStatus(state=ConnectionState.CONNECTING, ssid="home", ip_address=None))

    assert is_provisioned(network) is True


def test_is_provisioned_true_when_disconnected_but_a_network_is_known() -> None:
    # E.g. rebooted out of range of the home network: unprovisioned would
    # wrongly send this device back into the setup flow.
    network = FakeNetworkPort(known_networks={"home"})

    assert is_provisioned(network) is True


def test_require_provisioned_raises_when_unprovisioned() -> None:
    network = FakeNetworkPort()

    with pytest.raises(NotProvisionedError):
        require_provisioned(network)


def test_require_provisioned_is_silent_when_provisioned() -> None:
    network = FakeNetworkPort(known_networks={"home"})

    require_provisioned(network)  # must not raise
