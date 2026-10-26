"""``/api/v1/system``: status (always public), reboot, and shutdown."""

from __future__ import annotations

import socket
import time
from typing import Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from palmimo_portal.api.deps import (
    get_app_unit_port,
    get_clock_port,
    get_disk_port,
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
from palmimo_portal.core.apps_start import find_running_app
from palmimo_portal.core.identity import compute_auth_state
from palmimo_portal.ports import (
    AdapterUnavailableError,
    AppsStateFileState,
    AppUnitPort,
    ClockPort,
    DiskPort,
    Identity,
    IdentityStore,
    NetworkPort,
    StateStore,
    SystemPort,
)
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


class RunningAppInfo(BaseModel):
    id: str
    name: str
    state: Literal["running", "starting", "stopping"]


class WifiSetupInfo(BaseModel):
    mode: str
    screen: str | None
    reason: str | None = None


class SystemStatus(BaseModel):
    state: str
    hostname: str
    auth_state: str
    device_id: str | None
    versions: VersionInfo
    last_wifi_attempt: WifiAttemptInfo | None
    adapters: str
    state_dir: str
    ntp_synchronized: bool
    disk_free_bytes: int
    running_app: RunningAppInfo | None = None
    wifi_setup: WifiSetupInfo | None = None


def _running_app_for(request: Request, app_unit: AppUnitPort, state: StateStore) -> RunningAppInfo | None:
    if not getattr(request.state, "authenticated", False) or getattr(request.state, "session_mode", None) != "full":
        return None
    if state.apps_state_file_state() not in (AppsStateFileState.ABSENT, AppsStateFileState.PRESENT):
        return None
    try:
        running = find_running_app(app_unit, state.read_apps_state())
    except AdapterUnavailableError:
        return None
    if running is None:
        return None
    return RunningAppInfo(
        id=running.id,
        name=running.name,
        state=cast(Literal["running", "starting", "stopping"], running.state),
    )


class StatusResponse(BaseModel):
    status: str = "ok"


@router.get("/status")
def get_status(
    request: Request,
    network: NetworkPort = Depends(get_network_port),
    state: StateStore = Depends(get_state_store),
    identity_store: IdentityStore = Depends(get_identity_store),
    clock: ClockPort = Depends(get_clock_port),
    disk: DiskPort = Depends(get_disk_port),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
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

    ``ntp_synchronized`` and ``disk_free_bytes`` back the "clock not synced
    yet" / "disk almost full" UI warnings (design doc 3.6).

    ``running_app`` is ``None`` unless the caller holds a full session, so
    app names never reach an unauthenticated client, and it is ``None``
    rather than an error when systemd or the app ledger cannot be read --
    this endpoint must keep answering for every screen to render.
    """
    wifi_status = network.get_status()
    # Uncached: an operator watches this endpoint while diagnosing a device;
    # a removed/fixed identity file must reflect here without a restart.
    identity = identity_store.read_identity_uncached()
    auth_state = compute_auth_state(state.auth_state(), identity)
    last_attempt = state.read_last_wifi_attempt()
    settings: Settings = request.app.state.settings
    wifi_setup = request.app.state.wifi_setup.status()
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
        ntp_synchronized=clock.ntp_synchronized(),
        disk_free_bytes=disk.free_bytes(settings.state_dir),
        running_app=_running_app_for(request, app_unit, state),
        wifi_setup=WifiSetupInfo(mode=wifi_setup.mode, screen=wifi_setup.screen, reason=wifi_setup.reason),
    )


class ChecklistState(BaseModel):
    dismissed: bool


@router.get(
    "/checklist",
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)
def get_checklist(state: StateStore = Depends(get_state_store)) -> ChecklistState:
    """Whether Home's setup checklist is hidden. Its items are derived client-side from existing endpoints."""
    return ChecklistState(dismissed=state.read_checklist_dismissed())


@router.put(
    "/checklist",
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)
def put_checklist(body: ChecklistState, state: StateStore = Depends(get_state_store)) -> ChecklistState:
    """Hide or restore Home's setup checklist."""
    state.write_checklist_dismissed(body.dismissed)
    return ChecklistState(dismissed=body.dismissed)


def _refuse_while_updating(state: StateStore, runner_alive: bool, platform_runner_alive: bool) -> None:
    """Use self-healing Portal state so an abandoned runner cannot block power operations forever."""
    platform_job = state.read_platform_update_state().job
    if platform_runner_alive and platform_job.state == "running" and platform_job.step == "display_firmware":
        raise PortalError(409, "platform_update_in_progress")
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
            ``"running"``; 409 ``platform_update_in_progress`` during a
            live display firmware job.
    """
    _refuse_while_updating(
        state, request.app.state.update_runner_alive.is_set(), request.app.state.platform_job_runner.is_alive()
    )
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
            :func:`reboot`. 409 ``update_in_progress`` or
            ``platform_update_in_progress`` -- see :func:`reboot`.
    """
    _refuse_while_updating(
        state, request.app.state.update_runner_alive.is_set(), request.app.state.platform_job_runner.is_alive()
    )
    system.shutdown()
    return StatusResponse()
