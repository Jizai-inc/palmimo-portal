"""Unit tests for the pure Wi-Fi access decision rule."""

from __future__ import annotations

import pytest

from palmimo_portal.core.identity import PortalAuthState
from palmimo_portal.core.wifi_access import WifiAccessDecision, decide_wifi_access


def test_open_setup_and_unprovisioned_allows_an_unauthenticated_request() -> None:
    # The bootstrap case: a fresh DIY device with no password yet.
    decision = decide_wifi_access(PortalAuthState.OPEN_SETUP, authenticated=False, provisioned=False)

    assert decision is WifiAccessDecision.ALLOW


def test_open_setup_and_provisioned_denies_an_unauthenticated_request() -> None:
    decision = decide_wifi_access(PortalAuthState.OPEN_SETUP, authenticated=False, provisioned=True)

    assert decision is WifiAccessDecision.DENY


def test_open_setup_and_provisioned_allows_an_authenticated_request() -> None:
    decision = decide_wifi_access(PortalAuthState.OPEN_SETUP, authenticated=True, provisioned=True)

    assert decision is WifiAccessDecision.ALLOW


def test_open_setup_and_unprovisioned_allows_an_authenticated_request_too() -> None:
    decision = decide_wifi_access(PortalAuthState.OPEN_SETUP, authenticated=True, provisioned=False)

    assert decision is WifiAccessDecision.ALLOW


@pytest.mark.parametrize(
    "portal_state",
    [PortalAuthState.INITIAL, PortalAuthState.SET, PortalAuthState.CORRUPT, PortalAuthState.UNAVAILABLE],
)
@pytest.mark.parametrize("provisioned", [True, False])
def test_every_non_open_setup_state_denies_an_unauthenticated_request_regardless_of_provisioning(
    portal_state: PortalAuthState, provisioned: bool
) -> None:
    # The DIY bootstrap exception is exclusive to OPEN_SETUP -- an
    # identity-carrying device, a DIY device that already set a password,
    # a corrupt auth file, and an unreadable identity file all deny an
    # unauthenticated caller even while "unprovisioned" (e.g. the device
    # forgot its Wi-Fi network), so reopening Wi-Fi to the LAN never
    # happens just because provisioning state flips.
    decision = decide_wifi_access(portal_state, authenticated=False, provisioned=provisioned)

    assert decision is WifiAccessDecision.DENY


@pytest.mark.parametrize(
    "portal_state",
    [PortalAuthState.INITIAL, PortalAuthState.SET, PortalAuthState.CORRUPT, PortalAuthState.UNAVAILABLE],
)
@pytest.mark.parametrize("provisioned", [True, False])
def test_every_non_open_setup_state_allows_an_authenticated_request(
    portal_state: PortalAuthState, provisioned: bool
) -> None:
    decision = decide_wifi_access(portal_state, authenticated=True, provisioned=provisioned)

    assert decision is WifiAccessDecision.ALLOW
