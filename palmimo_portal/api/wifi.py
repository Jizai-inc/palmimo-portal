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

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from palmimo_portal.api.deps import (
    get_network_port,
    get_saved_network_port,
    get_wifi_attempts,
    require_auth,
    require_full_session,
    require_provisioned,
    require_wifi_access,
)
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core.wifi_attempt_manager import WifiAttemptManager
from palmimo_portal.ports import (
    AdapterUnavailableError,
    NetworkPort,
    NotConnectedError,
    SavedNetworkActiveError,
    SavedNetworkNotFoundError,
    SavedNetworkOrderError,
    SavedNetworkPort,
)


logger = logging.getLogger("palmimo_portal")

#: 802.11 bounds the raw wire SSID to 1..32 bytes, but this handler sees
#: NetworkManager/comitup's *decoded* name, not the raw bytes: a legal
#: 32-raw-byte SSID from an old Latin-1/SJIS router that isn't valid UTF-8
#: gets lossily decoded, each bad byte expanded to one U+FFFD (3 UTF-8
#: bytes) -- up to 96 encoded bytes for a network that works fine and is
#: already listed by ``GET /wifi/networks``. See
#: _SSID_LOSSY_DECODE_MAX_BYTES for the relaxed cap that exempts this case.
_SSID_MAX_BYTES = 32

#: Relaxed cap when the ssid contains U+FFFD (see _SSID_MAX_BYTES): the
#: connect target is matched against comitup's own scan names, so passing
#: the sanitized string through verbatim is what makes connecting to one of
#: these routers work -- a sanity ceiling against garbage, not a protocol limit.
_SSID_LOSSY_DECODE_MAX_BYTES = 128

#: The signature NetworkManager's lossy UTF-8 decode leaves behind: every
#: byte it couldn't decode becomes one U+FFFD.
_REPLACEMENT_CHAR = "�"

#: wpa_supplicant bounds a WPA2-PSK passphrase to 8..63 *bytes*, not
#: characters -- JP consumer routers commonly allow a UTF-8 passphrase, so
#: an ASCII-only rule would wrongly reject one that works fine on the device.
_PSK_MIN_BYTES = 8
_PSK_MAX_BYTES = 63


def _validate_ssid(ssid: str) -> None:
    """Reject an ``ssid`` that cannot survive comitup's D-Bus call or NetworkManager's own limits.

    A lone surrogate (e.g. a hand-crafted ``\\ud800`` escape -- stdlib
    ``json`` parses it into a ``str`` happily) cannot encode to UTF-8; left
    unchecked it would get persisted as the last Wi-Fi attempt and make
    every later ``GET /system/status`` 500 trying to serialize it back out.
    The C0-control/DEL check catches an embedded NUL, which breaks the
    D-Bus message. Byte cap is normally 32 (:data:`_SSID_MAX_BYTES`),
    relaxed to :data:`_SSID_LOSSY_DECODE_MAX_BYTES` when the string
    contains :data:`_REPLACEMENT_CHAR`.
    """
    try:
        encoded = ssid.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PortalError(400, "wifi_invalid_ssid") from error
    max_bytes = _SSID_LOSSY_DECODE_MAX_BYTES if _REPLACEMENT_CHAR in ssid else _SSID_MAX_BYTES
    if not (1 <= len(encoded) <= max_bytes):
        raise PortalError(400, "wifi_invalid_ssid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in ssid):
        raise PortalError(400, "wifi_invalid_ssid")


def _validate_psk(psk: str) -> None:
    """Reject a ``psk`` that isn't a valid WPA2 passphrase (or the empty string for an open network).

    Accepts an 8..63 *byte* passphrase (arbitrary UTF-8, not ASCII-only) or
    a 64-char hex raw key. Anything else would otherwise only be rejected
    by NetworkManager after the reconfigure path has already run ``forget``
    on the device's current network for a doomed request.
    """
    if psk == "":
        return
    if len(psk) == 64 and all(char in "0123456789abcdefABCDEF" for char in psk):
        return
    try:
        encoded = psk.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PortalError(400, "wifi_invalid_psk") from error
    if _PSK_MIN_BYTES <= len(encoded) <= _PSK_MAX_BYTES and not any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in psk
    ):
        return
    raise PortalError(400, "wifi_invalid_psk")


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


class SavedNetworkResponse(BaseModel):
    ssid: str
    active: bool


class SaveRequest(BaseModel):
    ssid: str
    psk: str


class SsidRequest(BaseModel):
    ssid: str


class OrderRequest(BaseModel):
    ssids: list[str]


class ActivateResponse(BaseModel):
    status: str = "attempting"


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
def connect(body: ConnectRequest, attempts: WifiAttemptManager = Depends(get_wifi_attempts)) -> ConnectResponse:
    """Start an attempt; its result is available from system status.

    Raises:
        PortalError: 400 for invalid credentials, 409 wifi_attempt_in_progress,
            502 wifi_connect_failed, or 503 wifi_kick_unavailable / wifi_cleanup_failed.
    """
    _validate_ssid(body.ssid)
    _validate_psk(body.psk)
    attempts.start_wifi_attempt("credentials", body.ssid.encode("utf-8"), body.psk)
    return ConnectResponse()


@router.delete(
    "/connection",
    dependencies=[Depends(require_provisioned), Depends(require_auth)],
)
def forget(
    network: NetworkPort = Depends(get_network_port), attempts: WifiAttemptManager = Depends(get_wifi_attempts)
) -> ForgetResponse:
    """Forget the currently connected network and drop the connection.

    Must never be reachable anonymously -- unlike the rest of this router,
    this route adds :func:`~palmimo_portal.api.deps.require_provisioned`
    and :func:`~palmimo_portal.api.deps.require_auth` explicitly on top of
    the router's own ``require_wifi_access`` + ``require_full_session``.
    Unlike ``POST /wifi/connect``, does *not* write a ``last_wifi_attempt``
    record -- forgetting is not a connect attempt.

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
    with attempts.saved_network_edit():
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


_saved_dependencies = [Depends(require_provisioned), Depends(require_auth)]


@router.get("/saved", dependencies=_saved_dependencies)
def list_saved(
    saved: SavedNetworkPort = Depends(get_saved_network_port), attempts: WifiAttemptManager = Depends(get_wifi_attempts)
) -> list[SavedNetworkResponse]:
    """Return the saved networks in the order the device tries them, first first."""
    visible = {p.ssid.decode("utf-8", errors="replace") for p in attempts.visible_profiles()}
    return [
        SavedNetworkResponse(ssid=item.ssid, active=item.active) for item in saved.list_saved() if item.ssid in visible
    ]


@router.post("/saved", dependencies=_saved_dependencies)
def save_network(
    body: SaveRequest,
    saved: SavedNetworkPort = Depends(get_saved_network_port),
    attempts: WifiAttemptManager = Depends(get_wifi_attempts),
) -> list[SavedNetworkResponse]:
    """Save a network without switching to it (last in the order), or replace a saved one's passphrase.

    Raises:
        PortalError: 400 ``wifi_invalid_ssid`` / ``wifi_invalid_psk`` (see :func:`connect`).
    """
    _validate_ssid(body.ssid)
    _validate_psk(body.psk)
    with attempts.saved_network_edit():
        saved.save(body.ssid, body.psk, first=False)
        return list_saved(saved, attempts)


@router.post("/saved/delete", dependencies=_saved_dependencies)
def delete_saved(
    body: SsidRequest,
    saved: SavedNetworkPort = Depends(get_saved_network_port),
    attempts: WifiAttemptManager = Depends(get_wifi_attempts),
) -> list[SavedNetworkResponse]:
    """Delete a saved network other than the current one.

    Raises:
        PortalError: 404 ``wifi_saved_not_found``. 409 ``wifi_saved_active`` for the
            current network -- ``DELETE /wifi/connection`` forgets that one.
    """
    try:
        with attempts.saved_network_edit():
            saved.delete(body.ssid)
    except SavedNetworkNotFoundError as error:
        raise PortalError(404, "wifi_saved_not_found") from error
    except SavedNetworkActiveError as error:
        raise PortalError(409, "wifi_saved_active") from error
    return list_saved(saved, attempts)


@router.put("/saved/order", dependencies=_saved_dependencies)
def reorder_saved(
    body: OrderRequest,
    saved: SavedNetworkPort = Depends(get_saved_network_port),
    attempts: WifiAttemptManager = Depends(get_wifi_attempts),
) -> list[SavedNetworkResponse]:
    """Set the order the device tries saved networks in, first first.

    Raises:
        PortalError: 409 ``wifi_saved_order_mismatch`` if ``ssids`` is not exactly
            the saved set (another client changed it meanwhile).
    """
    try:
        with attempts.saved_network_edit():
            saved.reorder(body.ssids)
    except SavedNetworkOrderError as error:
        raise PortalError(409, "wifi_saved_order_mismatch") from error
    return list_saved(saved, attempts)


@router.post("/saved/activate", dependencies=_saved_dependencies)
def activate_saved(body: SsidRequest, attempts: WifiAttemptManager = Depends(get_wifi_attempts)) -> ActivateResponse:
    """Attempt a saved network while retaining its original priority.

    Raises:
        PortalError: 404 wifi_saved_not_found, 409 wifi_attempt_in_progress,
            502 wifi_connect_failed, or 503 wifi_kick_unavailable / wifi_cleanup_failed.
    """
    _validate_ssid(body.ssid)
    try:
        attempts.activate_saved(body.ssid)
    except SavedNetworkNotFoundError as error:
        raise PortalError(404, "wifi_saved_not_found") from error
    return ActivateResponse()
