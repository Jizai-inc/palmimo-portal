"""The Wi-Fi endpoints' access-decision rule, in isolation from HTTP.

Kept as a pure function, unit-tested here in ``core/`` independent of
FastAPI. :func:`~palmimo_portal.api.deps.require_wifi_access` only calls
:func:`decide_wifi_access` and translates :attr:`WifiAccessDecision.DENY`
into the 401 envelope.
"""

from __future__ import annotations

from enum import StrEnum

from palmimo_portal.core.identity import PortalAuthState


class WifiAccessDecision(StrEnum):
    """The outcome of :func:`decide_wifi_access`."""

    ALLOW = "allow"
    DENY = "deny"


def decide_wifi_access(portal_state: PortalAuthState, *, authenticated: bool, provisioned: bool) -> WifiAccessDecision:
    """Decide whether a request to a Wi-Fi endpoint may proceed.

    Unauthenticated access while unprovisioned -- the bootstrap step for a
    fresh DIY device with no password yet -- is allowed *only* in
    :attr:`~palmimo_portal.core.identity.PortalAuthState.OPEN_SETUP` (no
    identity file, no password ever set). Every other state always denies
    an unauthenticated caller regardless of ``provisioned``: an
    identity-carrying device must sticker-login first; a DIY device that
    already set a password must not have Wi-Fi reopened to the LAN just
    by re-entering the unprovisioned state (e.g. forgetting its network)
    -- the bootstrap exception is only for a device that never had a
    password; ``corrupt``/``unavailable`` deny for the same reason.
    """
    if portal_state is PortalAuthState.OPEN_SETUP:
        if provisioned and not authenticated:
            return WifiAccessDecision.DENY
        return WifiAccessDecision.ALLOW
    if not authenticated:
        return WifiAccessDecision.DENY
    return WifiAccessDecision.ALLOW
