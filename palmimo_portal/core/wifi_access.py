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
    :attr:`~palmimo_portal.core.identity.PortalAuthState.OPEN_SETUP`: no
    identity file, and no password has ever been set
    (:attr:`~palmimo_portal.ports.AuthFileState.ABSENT`). Every other state
    always denies an unauthenticated caller, regardless of ``provisioned``:

    - **Identity-carrying device** (``initial`` or, once promoted, ``set``):
      the buyer must sticker-login (or log in normally) before Wi-Fi can be
      touched at all.
    - **DIY device with a password already set**: re-entering the
      unprovisioned state (e.g. it forgot its Wi-Fi network) must not
      reopen Wi-Fi to anyone on the LAN -- the bootstrap exception exists
      only for a device that has never had a password.
    - **``corrupt``/``unavailable``**: deny for the same reason -- neither
      is a legitimate bootstrap state.
    """
    if portal_state is PortalAuthState.OPEN_SETUP:
        if provisioned and not authenticated:
            return WifiAccessDecision.DENY
        return WifiAccessDecision.ALLOW
    if not authenticated:
        return WifiAccessDecision.DENY
    return WifiAccessDecision.ALLOW
