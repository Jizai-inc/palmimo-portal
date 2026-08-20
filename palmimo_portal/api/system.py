"""``/api/v1/system``: status (always public), reboot, and shutdown."""

from __future__ import annotations

import socket
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from palmimo_portal.api.deps import (
    get_identity_store,
    get_network_port,
    get_state_store,
    get_system_port,
    require_auth,
    require_full_session,
    require_provisioned,
)
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core import update as update_core
from palmimo_portal.core.identity import compute_auth_state
from palmimo_portal.ports import Identity, IdentityStore, NetworkPort, StateStore, SystemPort
from palmimo_portal.settings import Settings
from palmimo_portal.version import package_version, portal_version


router = APIRouter(prefix="/api/v1/system", tags=["system"])


class VersionInfo(BaseModel):
    portal: str
    sdk: str | None


class WifiAttemptInfo(BaseModel):
    ssid: str
    result: str
    timestamp: float
    observed_connection_name: str | None = None


class SystemStatus(BaseModel):
    state: str
    hostname: str
    auth_state: str
    device_id: str | None
    versions: VersionInfo
    last_wifi_attempt: WifiAttemptInfo | None
    adapters: str
    state_dir: str


class StatusResponse(BaseModel):
    status: str = "ok"


@router.get("/status")
def get_status(
    request: Request,
    network: NetworkPort = Depends(get_network_port),
    state: StateStore = Depends(get_state_store),
    identity_store: IdentityStore = Depends(get_identity_store),
) -> SystemStatus:
    """Report provisioning state, hostname, auth state, device id, versions, and the last Wi-Fi attempt.

    Always unauthenticated and always served, even while unprovisioned —
    this is the one endpoint every client (including the setup flow itself)
    polls to decide what to show.

    ``auth_state`` is one of ``"open_setup"`` (DIY, no identity file --
    legacy open ``/auth/setup`` available), ``"initial"`` (identity file
    present, no owner yet -- login with the sticker password), ``"set"``
    (normal operation), ``"corrupt"`` (``auth.json`` exists but cannot be
    read -- login and setup both refuse until an operator deletes it over
    SSH), or ``"unavailable"`` (identity file could not be read at all --
    a transient error, not clean absence -- so ``/auth/setup`` and
    ``/auth/login`` refuse with 503 until it clears) -- see
    :func:`~palmimo_portal.core.identity.compute_auth_state`.

    ``device_id`` is the number printed on the device's sticker (also its
    hostname suffix) -- public, reported whenever the identity file was
    successfully read, independent of ``auth_state``. ``None`` both when
    there is no identity file and when the read is currently unavailable.

    ``adapters`` and ``state_dir`` expose what this process is actually
    running as, so a device on fake adapters or the wrong state directory
    is diagnosable from a client request.
    """
    wifi_status = network.get_status()
    # Uncached: an operator watches this endpoint while diagnosing a device;
    # a removed/fixed identity file must reflect here without a restart.
    identity = identity_store.read_identity_uncached()
    auth_state = compute_auth_state(state.auth_state(), identity)
    last_attempt = state.read_last_wifi_attempt()
    settings: Settings = request.app.state.settings
    return SystemStatus(
        state=wifi_status.state.value,
        hostname=socket.gethostname(),
        auth_state=auth_state.value,
        device_id=identity.device_id if isinstance(identity, Identity) else None,
        versions=VersionInfo(portal=portal_version(), sdk=package_version("palmimo-sdk")),
        last_wifi_attempt=(
            WifiAttemptInfo(
                ssid=last_attempt.ssid,
                result=last_attempt.result,
                timestamp=last_attempt.timestamp,
                observed_connection_name=last_attempt.observed_connection_name,
            )
            if last_attempt is not None
            else None
        ),
        adapters=settings.adapters,
        state_dir=str(settings.state_dir),
    )


def _refuse_while_updating(state: StateStore, runner_alive: bool) -> None:
    """Raise 409 ``update_in_progress`` while an update/rollback job is ``"running"``.

    Goes through :func:`~palmimo_portal.core.update.current_update_state`
    (the same self-healing sequence ``GET /update/status`` runs), not a raw
    read -- a ``"running"`` job left over from a dead runner (see
    :func:`~palmimo_portal.core.update.expire_stale_running`) must not block
    reboot/shutdown forever. Not gated behind the update job lock, so a
    reboot/shutdown mid-``uv sync`` cannot turn a half-synced venv into a
    crash loop. Narrower than "any job in flight": ``"checking"`` is a
    synchronous sub-10s call with nothing left running by the time this
    executes, and ``"restarting"`` means the update already fully applied.
    """
    if update_core.current_update_state(state, now=time.time(), runner_alive=runner_alive).job.state == "running":
        raise PortalError(409, "update_in_progress")


@router.post(
    "/reboot",
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)
def reboot(
    request: Request, system: SystemPort = Depends(get_system_port), state: StateStore = Depends(get_state_store)
) -> StatusResponse:
    """Reboot the machine.

    Raises:
        PortalError: 503 ``system_backend_unavailable`` if the real
            adapter's D-Bus call to logind times out or fails. Translated
            from :class:`~palmimo_portal.ports.AdapterUnavailableError` by
            the app-wide exception handler in :mod:`palmimo_portal.api.app`.
            409 ``update_in_progress`` while an update/rollback job is
            ``"running"`` -- see :func:`_refuse_while_updating`.
    """
    _refuse_while_updating(state, request.app.state.update_runner_alive.is_set())
    system.reboot()
    return StatusResponse()


@router.post(
    "/shutdown",
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)
def shutdown(
    request: Request, system: SystemPort = Depends(get_system_port), state: StateStore = Depends(get_state_store)
) -> StatusResponse:
    """Shut the machine down safely.

    Raises:
        PortalError: 503 ``system_backend_unavailable`` -- see
            :func:`reboot`. 409 ``update_in_progress`` -- see
            :func:`_refuse_while_updating`.
    """
    _refuse_while_updating(state, request.app.state.update_runner_alive.is_set())
    system.shutdown()
    return StatusResponse()
