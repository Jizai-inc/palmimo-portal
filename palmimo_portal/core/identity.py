"""The combined auth-state classification: ``auth.json`` plus the identity file.

Extends the ABSENT/PRESENT/CORRUPT of ``auth.json`` (see
:mod:`palmimo_portal.adapters.state`) with what the manufacturing identity
file adds. A device with no owner yet is either ``open_setup`` (a DIY,
self-flashed image with no identity file -- the legacy unauthenticated
first-time-setup flow) or ``initial`` (an identity file is present -- login
only with the device's sticker password, gated until it is changed).
"""

from __future__ import annotations

from enum import StrEnum

from palmimo_portal.ports import IDENTITY_UNAVAILABLE, AuthFileState, Identity, IdentityUnavailable


class PortalAuthState(StrEnum):
    """The five states ``GET /api/v1/system/status`` reports as ``auth_state``."""

    OPEN_SETUP = "open_setup"
    """No identity file and ``auth.json`` is :attr:`~palmimo_portal.ports.AuthFileState.ABSENT`
    -- the DIY out-of-box state. The legacy unauthenticated ``/auth/setup`` is available."""

    INITIAL = "initial"
    """An identity file is present and ``auth.json`` is
    :attr:`~palmimo_portal.ports.AuthFileState.ABSENT`. Login with the
    device's sticker password only; every other endpoint is gated until
    the password is changed."""

    SET = "set"
    """``auth.json`` is :attr:`~palmimo_portal.ports.AuthFileState.PRESENT` -- normal operation."""

    CORRUPT = "corrupt"
    """``auth.json`` is :attr:`~palmimo_portal.ports.AuthFileState.CORRUPT`
    -- login and setup both refuse until an operator deletes the file over
    SSH. Takes priority over the identity file: an identity-carrying
    device with a corrupt ``auth.json`` must lock exactly like a DIY one,
    never fall back to the sticker password."""

    UNAVAILABLE = "unavailable"
    """The identity file could not be read (a transient error, not clean
    absence -- see :data:`~palmimo_portal.ports.IDENTITY_UNAVAILABLE`).
    Both ``/auth/setup`` and ``/auth/login`` refuse with 503
    ``identity_unavailable`` rather than guessing DIY vs. identity-carrying
    -- guessing "DIY" would let anyone claim a sticker/OEM device for as
    long as the read keeps failing (e.g. a slow ``/boot/firmware`` mount at
    startup)."""


def compute_auth_state(
    auth_file_state: AuthFileState, identity: Identity | IdentityUnavailable | None
) -> PortalAuthState:
    """Classify the device's overall auth state from ``auth.json`` and the identity file.

    ``auth_file_state`` is authoritative whenever ``auth.json`` exists:
    :attr:`~palmimo_portal.ports.AuthFileState.CORRUPT` always locks
    (identity or not), and :attr:`~palmimo_portal.ports.AuthFileState.PRESENT`
    always means :attr:`PortalAuthState.SET`. Only when ``auth.json`` is
    :attr:`~palmimo_portal.ports.AuthFileState.ABSENT` does the identity
    read decide between open-setup, the initial-credentials gate, and
    :attr:`PortalAuthState.UNAVAILABLE`.
    """
    if auth_file_state is AuthFileState.CORRUPT:
        return PortalAuthState.CORRUPT
    if auth_file_state is AuthFileState.PRESENT:
        return PortalAuthState.SET
    if identity is IDENTITY_UNAVAILABLE:
        return PortalAuthState.UNAVAILABLE
    return PortalAuthState.INITIAL if identity is not None else PortalAuthState.OPEN_SETUP
