"""``/api/v1/wifi``: status, nearby-network scan, and connect.

Auth requirement depends on the device, per
:func:`~palmimo_portal.api.deps.require_wifi_access`: unauthenticated while
unprovisioned on a DIY device (the setup flow itself), but always
session-gated on an identity-carrying device, provisioned or not. Never
gated by :func:`~palmimo_portal.api.deps.require_provisioned` directly --
these endpoints must stay reachable in the unprovisioned state for setup
to be possible at all.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from palmimo_portal.api.deps import (
    get_network_port,
    get_state_store,
    require_auth,
    require_full_session,
    require_provisioned,
    require_wifi_access,
)
from palmimo_portal.api.errors import PortalError
from palmimo_portal.ports import (
    AdapterUnavailableError,
    ConnectionState,
    NetworkPort,
    NotConnectedError,
    StateStore,
    WifiAttempt,
)


logger = logging.getLogger("palmimo_portal")

router = APIRouter(
    prefix="/api/v1/wifi",
    tags=["wifi"],
    dependencies=[Depends(require_wifi_access), Depends(require_full_session)],
)


class WifiStatusResponse(BaseModel):
    state: str
    ssid: str | None
    ip_address: str | None


class WifiNetworkResponse(BaseModel):
    ssid: str
    signal: int
    secured: bool


class ConnectRequest(BaseModel):
    ssid: str
    psk: str


class ConnectResponse(BaseModel):
    status: str = "attempting"


class ForgetResponse(BaseModel):
    status: str = "forgetting"


@router.get("/status")
def get_status(network: NetworkPort = Depends(get_network_port)) -> WifiStatusResponse:
    """Return the current connection state.

    Raises:
        PortalError: 503 ``network_backend_unavailable`` if the real
            adapter's D-Bus call to comitup times out or fails -- comitup
            not running, or the system bus unreachable. Translated from
            :class:`~palmimo_portal.ports.AdapterUnavailableError` by the
            app-wide exception handler in :mod:`palmimo_portal.api.app`,
            not caught here directly, since the same failure can equally
            surface from :func:`~palmimo_portal.api.deps.require_wifi_access`
            before this function body ever runs.
    """
    status = network.get_status()
    return WifiStatusResponse(state=status.state.value, ssid=status.ssid, ip_address=status.ip_address)


@router.get("/networks")
def list_networks(network: NetworkPort = Depends(get_network_port)) -> list[WifiNetworkResponse]:
    """Return the most recent nearby-SSID scan.

    Raises:
        PortalError: 503 ``network_backend_unavailable`` -- see
            :func:`get_status`.
    """
    return [
        WifiNetworkResponse(ssid=item.ssid, signal=item.signal, secured=item.secured)
        for item in network.list_networks()
    ]


@router.post("/connect")
def connect(
    body: ConnectRequest,
    network: NetworkPort = Depends(get_network_port),
    state: StateStore = Depends(get_state_store),
) -> ConnectResponse:
    """Start a connect attempt and return immediately.

    The AP-disconnection asymmetry (connecting to the home network tears
    down the setup AP the client is talking through) means the result can
    never come back on this response -- it is recorded via
    :class:`~palmimo_portal.ports.StateStore` and read back later through
    ``GET /api/v1/system/status``.

    The attempt is recorded *before* :meth:`~palmimo_portal.ports.NetworkPort.connect`
    is called: if ``connect`` raises or the process dies right after, a
    record written afterward would vanish. If ``connect`` raises, the
    attempt is corrected to a durable "failed" result rather than left
    claiming "attempting" forever. This does *not* transition "attempting"
    to "success" asynchronously -- that belongs to the real network
    adapter, which observes the actual connection outcome.

    Raises:
        PortalError: 502 ``wifi_connect_failed`` if ``network.connect``
            itself raises (e.g. the radio is busy).

    Note: if the device is currently ``CONNECTED``, the real adapter
    forgets that network before connecting to the new one (see
    :meth:`~palmimo_portal.adapters.comitup.ComitupNetworkPort.connect`) --
    comitup would otherwise short-circuit back to the old network. This
    handler reads :meth:`~palmimo_portal.ports.NetworkPort.get_status`
    first purely for operator visibility: a WARNING is logged naming both
    the network about to be forgotten and the one being connected to.
    """
    current = network.get_status()
    old_ssid = current.ssid if current.state is ConnectionState.CONNECTED else None
    if old_ssid is not None:
        logger.warning("wifi reconfigure: forgetting %r to connect to %r", old_ssid, body.ssid)
    state.write_last_wifi_attempt(WifiAttempt(ssid=body.ssid, result="attempting", timestamp=time.time()))
    try:
        network.connect(body.ssid, body.psk)
    except Exception as error:
        if old_ssid is not None:
            logger.error("wifi connect to %r (forgetting %r) failed", body.ssid, old_ssid, exc_info=True)
        else:
            logger.error("wifi connect to %r failed", body.ssid, exc_info=True)
        state.write_last_wifi_attempt(WifiAttempt(ssid=body.ssid, result="failed", timestamp=time.time()))
        raise PortalError(502, "wifi_connect_failed") from error
    return ConnectResponse()


@router.delete(
    "/connection",
    dependencies=[Depends(require_provisioned), Depends(require_auth)],
)
def forget(network: NetworkPort = Depends(get_network_port)) -> ForgetResponse:
    """Forget the currently connected network and drop the connection.

    Forgetting must never be reachable anonymously -- unlike the rest of
    this router, a device with no network yet has nothing to forget, so
    this route adds :func:`~palmimo_portal.api.deps.require_provisioned`
    and :func:`~palmimo_portal.api.deps.require_auth` explicitly on top of
    the router's own ``require_wifi_access`` + ``require_full_session``.

    Unlike ``POST /wifi/connect``, this does *not* write a
    ``last_wifi_attempt`` record -- forgetting is not a connect attempt.

    Raises:
        PortalError: 503 ``network_backend_unavailable`` if
            ``network.forget_current`` raises
            :class:`~palmimo_portal.ports.AdapterUnavailableError` --
            translated by the app-wide exception handler in
            :mod:`palmimo_portal.api.app`. 409 ``wifi_not_connected`` if
            it raises :class:`~palmimo_portal.ports.NotConnectedError` --
            the adapter's own fresh state read (never this endpoint's
            ``status`` above, which can already be stale) found the
            device not actually ``CONNECTED``. 502 ``wifi_forget_failed``
            for any other exception.
    """
    status = network.get_status()
    logger.warning("wifi forget requested for current network %r", status.ssid)
    try:
        network.forget_current()
    except AdapterUnavailableError:
        raise
    except NotConnectedError as error:
        raise PortalError(409, "wifi_not_connected") from error
    except Exception as error:
        logger.error("wifi forget_current for %r failed", status.ssid, exc_info=True)
        raise PortalError(502, "wifi_forget_failed") from error
    return ForgetResponse()
