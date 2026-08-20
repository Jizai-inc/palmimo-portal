"""Unit tests for :func:`~palmimo_portal.core.identity.compute_auth_state`."""

from __future__ import annotations

from palmimo_portal.core.identity import PortalAuthState, compute_auth_state
from palmimo_portal.ports import IDENTITY_UNAVAILABLE, AuthFileState, Identity


IDENTITY = Identity(device_id="palmimo-042", initial_password="sticker-password")


def test_open_setup_when_no_identity_and_auth_absent() -> None:
    assert compute_auth_state(AuthFileState.ABSENT, None) is PortalAuthState.OPEN_SETUP


def test_initial_when_identity_present_and_auth_absent() -> None:
    assert compute_auth_state(AuthFileState.ABSENT, IDENTITY) is PortalAuthState.INITIAL


def test_set_when_auth_present_and_no_identity() -> None:
    assert compute_auth_state(AuthFileState.PRESENT, None) is PortalAuthState.SET


def test_set_when_auth_present_and_identity_present() -> None:
    # An identity-carrying device that already changed its password stays
    # "set" -- the identity file does not reopen initial mode once an
    # owner exists.
    assert compute_auth_state(AuthFileState.PRESENT, IDENTITY) is PortalAuthState.SET


def test_corrupt_when_auth_corrupt_and_no_identity() -> None:
    assert compute_auth_state(AuthFileState.CORRUPT, None) is PortalAuthState.CORRUPT


def test_corrupt_takes_priority_over_identity() -> None:
    # A corrupt auth.json must lock exactly like a DIY device, not fall
    # back to the sticker password just because an identity file exists.
    assert compute_auth_state(AuthFileState.CORRUPT, IDENTITY) is PortalAuthState.CORRUPT


def test_unavailable_when_identity_read_fails_and_auth_absent() -> None:
    # The identity file could not be read at all (e.g. /boot/firmware not
    # mounted yet) -- this must not be treated as "no identity file", which
    # would open the DIY unauthenticated setup flow on a sticker device.
    assert compute_auth_state(AuthFileState.ABSENT, IDENTITY_UNAVAILABLE) is PortalAuthState.UNAVAILABLE


def test_corrupt_takes_priority_over_unavailable() -> None:
    assert compute_auth_state(AuthFileState.CORRUPT, IDENTITY_UNAVAILABLE) is PortalAuthState.CORRUPT


def test_set_takes_priority_over_unavailable() -> None:
    # An owner already exists (auth.json PRESENT) -- a transient identity
    # read failure must not demote a device that already has a password
    # set back to a locked "unavailable" state.
    assert compute_auth_state(AuthFileState.PRESENT, IDENTITY_UNAVAILABLE) is PortalAuthState.SET
