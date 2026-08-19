"""FastAPI dependency providers shared by the ``api/`` routers.

Kept separate from :mod:`palmimo_portal.api.app` to avoid a circular
import: ``app.py`` imports the routers, and the routers need these
providers.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import Depends, Request

from palmimo_portal.api.errors import PortalError
from palmimo_portal.core.auth import LoginRateLimiter, ResetRateLimiter
from palmimo_portal.core.identity import PortalAuthState, compute_auth_state
from palmimo_portal.core.provisioning import NotProvisionedError, is_provisioned
from palmimo_portal.core.provisioning import require_provisioned as _core_require_provisioned
from palmimo_portal.core.wifi_access import WifiAccessDecision, decide_wifi_access
from palmimo_portal.ports import (
    AuthFileState,
    IdentityStore,
    NetworkPort,
    ReleaseSource,
    SshKeyPort,
    StateStore,
    SystemPort,
    Updater,
)


def get_network_port(request: Request) -> NetworkPort:
    """Return the wired :class:`NetworkPort` adapter."""
    adapters: Any = request.app.state.adapters
    network: NetworkPort = adapters.network
    return network


def get_system_port(request: Request) -> SystemPort:
    """Return the wired :class:`SystemPort` adapter."""
    adapters: Any = request.app.state.adapters
    system: SystemPort = adapters.system
    return system


def get_ssh_key_port(request: Request) -> SshKeyPort:
    """Return the wired :class:`SshKeyPort` adapter."""
    adapters: Any = request.app.state.adapters
    ssh_keys: SshKeyPort = adapters.ssh_keys
    return ssh_keys


def get_state_store(request: Request) -> StateStore:
    """Return the wired :class:`StateStore` adapter."""
    adapters: Any = request.app.state.adapters
    state: StateStore = adapters.state
    return state


def get_rate_limiter(request: Request) -> LoginRateLimiter:
    """Return the app-wide login rate limiter."""
    limiter: LoginRateLimiter = request.app.state.rate_limiter
    return limiter


def get_reset_rate_limiter(request: Request) -> ResetRateLimiter:
    """Return the app-wide login-credentials-reset throttle (see :class:`ResetRateLimiter`).

    A separate instance from :func:`get_rate_limiter`'s
    :class:`~palmimo_portal.core.auth.LoginRateLimiter`: the two protect
    different things and must not share one budget.
    """
    limiter: ResetRateLimiter = request.app.state.reset_rate_limiter
    return limiter


def get_identity_store(request: Request) -> IdentityStore:
    """Return the wired :class:`IdentityStore` adapter."""
    adapters: Any = request.app.state.adapters
    identity: IdentityStore = adapters.identity
    return identity


def get_release_source(request: Request) -> ReleaseSource:
    """Return the wired :class:`ReleaseSource` adapter."""
    adapters: Any = request.app.state.adapters
    releases: ReleaseSource = adapters.releases
    return releases


def get_updater(request: Request) -> Updater:
    """Return the wired :class:`Updater` adapter."""
    adapters: Any = request.app.state.adapters
    updater: Updater = adapters.updater
    return updater


def get_update_lock(request: Request) -> threading.Lock:
    """Return the app-wide lock serializing update check/apply/rollback state transitions.

    Held for the whole body of ``POST /update/check`` (including the
    GitHub round trip) and across the read-transition-write of
    ``POST /update/apply``/``POST /update/rollback`` so two concurrent
    requests cannot both observe an idle job and both start one.
    """
    lock: threading.Lock = request.app.state.update_lock
    return lock


def require_auth(request: Request) -> None:
    """Reject the request unless :class:`~palmimo_portal.api.app.SessionMiddleware` found a valid session.

    Raises:
        PortalError: 401 ``not_authenticated``.
    """
    if not getattr(request.state, "authenticated", False):
        raise PortalError(401, "not_authenticated")


def require_full_session(request: Request) -> None:
    """Reject the request unless the current session is full-mode.

    Applies to every authenticated endpoint except
    ``POST /auth/change-password`` and ``POST /auth/logout``: a session
    issued while ``auth_state == "initial"`` can do nothing else until the
    password is changed, so a device is never left running with the
    sticker password still active. Accepts only ``session_mode == "full"``
    and rejects any other *present* mode -- allowlisting rather than
    denylisting just ``"initial"``, so an unrecognized future mode fails
    closed. An absent mode (``None``, no session verified) still passes
    through untouched -- left to whichever auth dependency precedes this
    one (:func:`require_auth` for most routers, :func:`require_wifi_access`
    for Wi-Fi, which allows anonymous access while a DIY device is
    unprovisioned).

    Raises:
        PortalError: 403 ``initial_password_must_be_changed``.
    """
    session_mode = getattr(request.state, "session_mode", None)
    if session_mode is not None and session_mode != "full":
        raise PortalError(403, "initial_password_must_be_changed")


def require_provisioned(request: Request, network: NetworkPort = Depends(get_network_port)) -> None:
    """Reject the request unless the device has left the unprovisioned state.

    Applies to every endpoint except the Wi-Fi endpoints, ``auth/setup``,
    and ``system/status`` — those stay reachable so a fresh device can be
    set up at all.

    A thin HTTP translation over the one canonical rule --
    :func:`~palmimo_portal.core.provisioning.require_provisioned` -- so
    that rule stays the only place the check can drift.

    Raises:
        PortalError: 409 ``not_provisioned``.
    """
    try:
        _core_require_provisioned(network)
    except NotProvisionedError as error:
        raise PortalError(409, "not_provisioned") from error


def require_provisioned_unless_identity(
    request: Request,
    network: NetworkPort = Depends(get_network_port),
    identity_store: IdentityStore = Depends(get_identity_store),
    state: StateStore = Depends(get_state_store),
) -> None:
    """Gate ``auth/login``, ``auth/logout``, and ``auth/change-password`` on provisioning -- with two exceptions.

    On a DIY (open-setup) device that has never set a password, this is
    identical to :func:`require_provisioned`: login is not needed until
    after Wi-Fi is configured, since a DIY owner already has physical/SSH
    access. Two cases skip the provisioning check entirely instead:

    - **An identity file is present.** Login is the *only* way to obtain a
      session on such a device, and the sticker/initial-password flow
      necessarily happens before Wi-Fi is ever configured (the wifi
      endpoints become session-gated once an identity file exists -- see
      :func:`require_wifi_access`). Requiring provisioning before login
      here would make it impossible to provision at all.
    - **A password is already set** (``auth.json`` is
      :attr:`~palmimo_portal.ports.AuthFileState.PRESENT`), even without
      an identity file. A DIY device that completed setup and later
      re-enters the unprovisioned state (e.g. it forgot its Wi-Fi network)
      must still be able to log back in: :func:`require_wifi_access`
      always session-gates such a device, so without this exception it
      could never obtain the session Wi-Fi requires -- a deadlock.

    Raises:
        PortalError: 409 ``not_provisioned`` -- only when neither exception applies.
    """
    # `is not None` is true for both a real Identity and IDENTITY_UNAVAILABLE
    # (a str, never None) -- an unavailable read is treated the same as
    # "identity file present" here, not as "no identity file" (which would
    # fall through to require_provisioned, the DIY path). That's the safe
    # side to fail on: login itself (api/auth.py) makes its own
    # compute_auth_state() check and refuses with 503 identity_unavailable
    # before touching a password, so letting the request past this gate
    # cannot open anything.
    if identity_store.read_identity() is not None:
        return
    if state.auth_state() is AuthFileState.PRESENT:
        return
    require_provisioned(request, network)


def require_wifi_access(
    request: Request,
    network: NetworkPort = Depends(get_network_port),
    identity_store: IdentityStore = Depends(get_identity_store),
    state: StateStore = Depends(get_state_store),
) -> None:
    """Gate the Wi-Fi endpoints.

    A thin HTTP translation over the pure decision rule in
    :func:`~palmimo_portal.core.wifi_access.decide_wifi_access` -- see its
    docstring for the rule itself. This function only gathers that rule's
    three inputs and maps
    :attr:`~palmimo_portal.core.wifi_access.WifiAccessDecision.DENY` onto
    the 401 envelope.

    Raises:
        PortalError: 401 ``not_authenticated``.
    """
    identity = identity_store.read_identity()
    portal_state = compute_auth_state(state.auth_state(), identity)
    authenticated = getattr(request.state, "authenticated", False)
    # is_provisioned(network) only matters to decide_wifi_access for
    # OPEN_SETUP -- calling it only in that case avoids an extra
    # NetworkPort round-trip elsewhere, where the answer can't change the
    # decision.
    provisioned = is_provisioned(network) if portal_state is PortalAuthState.OPEN_SETUP else False
    decision = decide_wifi_access(portal_state, authenticated=authenticated, provisioned=provisioned)
    if decision is WifiAccessDecision.DENY:
        raise PortalError(401, "not_authenticated")
