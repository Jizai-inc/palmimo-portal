"""Real :class:`~palmimo_portal.ports.NetworkPort`: comitup 1.43's D-Bus service.

Bus name/path/interface: ``com.github.davesteele.comitup`` (verified against
a real comitup 1.43 install). Methods used: ``state() -> ss`` (comitup's own
``HOTSPOT``/``CONNECTING``/``CONNECTED`` state plus the connection/AP name);
``access_points() -> aa{ss}`` (a live scan, can take several seconds);
``connect(ssid, psk)`` (async, result only observable later via
:meth:`get_status`); ``delete_connection()`` (deletes the active SSID's
NetworkManager profile; comitup falls back to HOTSPOT or another known
network). ``nuke()`` is unused. comitup emits **no D-Bus signals** -- every
call here is polled -- and keeps no journal of its own transitions, so this
adapter logs every one it observes (:meth:`_log_transition`), or a fallback
to the hotspot would be invisible in ``journalctl``.

**Known-networks tracking.** comitup has no "list saved connections" call,
so :meth:`has_known_networks` keeps its own record: marks "known" on
``CONNECTING``/``CONNECTED`` or a successful :meth:`connect` (mirroring
:class:`~palmimo_portal.testing.fakes.FakeNetworkPort`), persisted as a
marker file so it survives a restart -- otherwise "rebooted out of range of
its home network" would misread as never-provisioned by
:func:`~palmimo_portal.core.provisioning.is_provisioned`. A factory reset
clears it alongside ``auth.json``.

**Why forget precedes connect.** comitup's ``connecting_start`` short-circuits
back to the *currently active* SSID if it is itself a candidate for the new
``connect()`` call, so a plain ``connect("Cafe", ...)`` while ``CONNECTED``
to ``Home-5G`` does nothing. :meth:`connect` therefore reads comitup's state
fresh immediately before deciding, and if ``CONNECTED``, deletes the old
profile itself (the same call :meth:`forget_current` makes) before calling
comitup's ``connect()``, leaving nothing to short-circuit back to.
Deliberate trade-off: the device drops the old LAN even if the new
connection then fails, falling back to comitup's own hotspot.
:class:`~palmimo_portal.testing.fakes.FakeNetworkPort` mirrors this rule.

**Never decide from a cached state.** :meth:`connect` and
:meth:`forget_current` poll ``state()`` fresh right before deciding, never
reusing what :meth:`_log_transition` last cached for :meth:`get_status`. A
stale ``CONNECTED`` reading would make this adapter ``delete_connection()``
while comitup is actually ``HOTSPOT`` -- deleting comitup's own hotspot
profile. A stale ``HOTSPOT`` reading would make :meth:`forget_current` skip
an actually-active connection instead of raising
:class:`~palmimo_portal.ports.NotConnectedError`.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface

from palmimo_portal.adapters.dbus_support import SharedEventLoopThread, get_shared_loop_thread
from palmimo_portal.core.wifi_attempt import resolve_attempt
from palmimo_portal.ports import (
    AdapterUnavailableError,
    ConnectionState,
    NetworkPort,
    NotConnectedError,
    StateStore,
    WifiAttempt,
    WifiNetwork,
    WifiStatus,
)


logger = logging.getLogger("palmimo_portal")

COMITUP_BUS_NAME = "com.github.davesteele.comitup"
COMITUP_OBJECT_PATH = "/com/github/davesteele/comitup"
COMITUP_INTERFACE = "com.github.davesteele.comitup"

# state()/get_info() are quick property-style reads; access_points() forces
# a live radio scan, measured on real hardware to take several seconds.
# Both are bounded -- a hung comitup process must surface as a 503 within a
# predictable time, not hang the request forever.
STATUS_CALL_TIMEOUT_SECONDS = 5.0
SCAN_CALL_TIMEOUT_SECONDS = 30.0

#: Bounds :meth:`ComitupNetworkPort._connect`'s bus-open sequence
#: (:meth:`~ComitupNetworkPort._open_bus`), independent of the eventual RPC
#: call's own timeout.
CONNECT_TIMEOUT_SECONDS = 5.0

_ADAPTER_ERROR_CODE = "network_backend_unavailable"

_STATE_MAP: dict[str, ConnectionState] = {
    "CONNECTED": ConnectionState.CONNECTED,
    "CONNECTING": ConnectionState.CONNECTING,
    "HOTSPOT": ConnectionState.UNPROVISIONED,
}

_UNSECURED_SECURITY_VALUES = frozenset({"", "none", "open", "--"})

_SIOCGIFADDR = 0x8915


def map_comitup_state(state: str) -> ConnectionState:
    """Map one of comitup's own state names to :class:`ConnectionState`.

    An unrecognized value maps to :attr:`ConnectionState.UNPROVISIONED`
    rather than raising -- the same fail-safe direction as comitup's own
    ``HOTSPOT`` state; ``has_known_networks`` rescues a provisioned device
    from being misread this way.
    """
    return _STATE_MAP.get(state, ConnectionState.UNPROVISIONED)


def parse_signal_strength(strength: str) -> int:
    """Parse ``access_points()``'s string-typed ``strength`` field into an int.

    A value that fails to parse becomes ``0`` rather than raising -- one
    malformed entry must not fail the whole
    :meth:`~ComitupNetworkPort.list_networks` call.
    """
    try:
        return int(strength)
    except (TypeError, ValueError):
        return 0


def parse_security(security: str) -> bool:
    """Parse ``access_points()``'s string-typed ``security`` field into ``secured: bool``."""
    return security.strip().lower() not in _UNSECURED_SECURITY_VALUES


def parse_access_points(records: list[dict[str, str]]) -> list[WifiNetwork]:
    """Map ``access_points()``'s raw string-dict list to :class:`WifiNetwork` entries.

    An entry with no (or empty) ``ssid`` is dropped -- a hidden network
    comitup cannot name isn't something a user can pick from a list.
    """
    networks = []
    for record in records:
        ssid = record.get("ssid", "")
        if not ssid:
            continue
        networks.append(
            WifiNetwork(
                ssid=ssid,
                signal=parse_signal_strength(record.get("strength", "")),
                secured=parse_security(record.get("security", "")),
            )
        )
    return networks


def _read_wlan_ipv4(interface: str) -> str | None:
    """Best-effort read of *interface*'s bound IPv4 address via ``SIOCGIFADDR`` (Linux only).

    Same technique as :mod:`palmimo_portal.api.app`'s
    ``_interface_ipv4_addresses`` but scoped to a single named interface.
    Any failure is swallowed: the IP address is a convenience on the status
    response, not worth turning a successful ``state()`` read into a 503.
    """
    try:
        import fcntl

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            packed = fcntl.ioctl(
                probe.fileno(),
                _SIOCGIFADDR,
                struct.pack("256s", interface[:15].encode("ascii")),
            )
        return socket.inet_ntoa(packed[20:24])
    except (ImportError, AttributeError, OSError):
        return None


class ComitupNetworkPort(NetworkPort):
    """Talks to comitup over D-Bus. See the module docstring for the full contract.

    ``_call`` does one attempt; ``_call_resilient`` wraps it with a
    reconnect-and-retry-once policy (the seam tests stub). Every public
    method funnels through :meth:`_call_sync`, bridging onto
    :mod:`dbus_fast`'s asyncio API via the shared background event loop
    (:mod:`palmimo_portal.adapters.dbus_support`).

    **Concurrency.** FastAPI's threadpool can run several requests through
    this instance at once. ``self._bus``/``self._interface`` is mutable
    shared state, so :attr:`_lock` serializes read-or-open in
    :meth:`_connect` and read-or-clear in :meth:`_disconnect` -- but not the
    RPC call itself (``access_points()`` can take up to
    :data:`SCAN_CALL_TIMEOUT_SECONDS`; serializing every call would block an
    unrelated ``state()`` poll behind a live scan). :meth:`_disconnect`
    re-checks under the lock that ``self._bus`` is still the object it was
    called with, so a call failing against an already-superseded bus cannot
    tear down a concurrent call's successful reconnect.
    """

    def __init__(
        self,
        *,
        known_network_marker: Path | None = None,
        wlan_interface: str = "wlan0",
        loop_thread: SharedEventLoopThread | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self._known_network_marker = known_network_marker
        self._wlan_interface = wlan_interface
        self._loop_thread = loop_thread if loop_thread is not None else get_shared_loop_thread()
        self._state_store = state_store
        self._bus: MessageBus | None = None
        self._interface: ProxyInterface | None = None
        self._known_in_memory = False
        self._last_logged: tuple[str, str] | None = None
        self._state_lock = threading.Lock()
        # asyncio.Lock does not bind to a loop at construction time (Python
        # 3.10+), so eager creation here is safe even though this
        # instance's coroutines only run on the shared background loop.
        self._lock = asyncio.Lock()

    async def _open_bus(self) -> tuple[MessageBus, ProxyInterface]:
        """Open a brand-new bus connection and resolve the comitup interface on it. Concurrency-test seam."""
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            introspection = await bus.introspect(COMITUP_BUS_NAME, COMITUP_OBJECT_PATH)
            proxy_object = bus.get_proxy_object(COMITUP_BUS_NAME, COMITUP_OBJECT_PATH, introspection)
            interface = proxy_object.get_interface(COMITUP_INTERFACE)
        except BaseException:
            # BaseException, not Exception: must also run on
            # asyncio.CancelledError, which _connect()'s asyncio.wait_for
            # raises here once CONNECT_TIMEOUT_SECONDS elapses (a
            # BaseException since Python 3.8) -- otherwise a bus that hung
            # in introspect() past the timeout would leak its fd.
            try:
                bus.disconnect()
            except Exception:
                logger.debug("comitup: error disconnecting a bus that failed to initialize (ignored)", exc_info=True)
            raise
        return bus, interface

    async def _connect(self) -> tuple[ProxyInterface, MessageBus]:
        """Return the cached ``(interface, bus)`` pair, connecting lazily. Bounded by :data:`CONNECT_TIMEOUT_SECONDS`, independent of the RPC call's own timeout."""
        async with self._lock:
            if self._interface is not None and self._bus is not None:
                return self._interface, self._bus
            bus, interface = await asyncio.wait_for(self._open_bus(), timeout=CONNECT_TIMEOUT_SECONDS)
            self._bus, self._interface = bus, interface
            return interface, bus

    async def _disconnect(self, bus: MessageBus) -> None:
        """Drop the cached bus/interface, but only if *bus* is still current. See class docstring's Concurrency note."""
        async with self._lock:
            if self._bus is not bus:
                return
            self._bus = None
            self._interface = None
        try:
            bus.disconnect()
        except Exception:  # best-effort -- the bus may already be dead
            logger.debug("comitup: error disconnecting a stale bus connection (ignored)", exc_info=True)

    async def _call(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        """Invoke one comitup D-Bus method by name. On an RPC failure (never a timeout), disconnects the bus this call used, then re-raises, so the retry reconnects from scratch."""
        interface, bus = await self._connect()
        method = getattr(interface, f"call_{member}")
        try:
            return await asyncio.wait_for(method(*args), timeout=timeout)
        except TimeoutError:
            raise
        except Exception:
            await self._disconnect(bus)
            raise

    async def _call_resilient(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        """Call :meth:`_call`, reconnecting and retrying exactly once on failure -- but never on a timeout.

        comitup restarting drops the D-Bus connection out from under a
        cached proxy; without this, every call after that would keep
        failing against a connection this process could simply reconnect.
        A malformed-call error is retried the same as a transport failure
        -- the retry is cheap and the two aren't reliably distinguishable
        from the exception alone.

        A :class:`TimeoutError` is never retried: the outer bridge
        (:meth:`_call_sync`) only grants ``timeout + 1.0`` seconds total, so
        a retry needing up to *timeout* seconds again could never complete
        within that budget -- it would just guarantee a slower failure.

        Raises:
            AdapterUnavailableError: the call timed out, or both the
                original call and the retry failed.
        """
        try:
            return await self._call(member, args, timeout)
        except TimeoutError as error:
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"comitup {member}() timed out") from error
        except Exception as first_error:
            logger.warning("comitup D-Bus call %r failed, reconnecting and retrying once: %s", member, first_error)
            try:
                return await self._call(member, args, timeout)
            except Exception as second_error:
                raise AdapterUnavailableError(
                    _ADAPTER_ERROR_CODE, f"comitup {member}() failed after a reconnect attempt: {second_error}"
                ) from second_error

    def _call_sync(self, member: str, *args: Any, timeout: float) -> Any:
        """Run :meth:`_call_resilient` on the shared background loop and block for the result."""
        try:
            return self._loop_thread.run(self._call_resilient(member, args, timeout), timeout=timeout + 1.0)
        except AdapterUnavailableError:
            raise
        except Exception as error:
            # A bridge-level failure, not one _call_resilient already
            # classified -- still adapter-unavailable from api/'s view.
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"comitup {member}() failed: {error}") from error

    def get_status(self) -> WifiStatus:
        state_str, name = self._call_sync("state", timeout=STATUS_CALL_TIMEOUT_SECONDS)
        is_first_observation = self._log_transition(state_str, name)
        self._resolve_pending_attempt(state_str, name, is_first_observation=is_first_observation)
        mapped = map_comitup_state(state_str)
        if mapped is not ConnectionState.UNPROVISIONED:
            self._mark_known()
        ip_address = _read_wlan_ipv4(self._wlan_interface) if mapped is ConnectionState.CONNECTED else None
        ssid = name if mapped is not ConnectionState.UNPROVISIONED else None
        return WifiStatus(state=mapped, ssid=ssid, ip_address=ip_address)

    def list_networks(self) -> list[WifiNetwork]:
        records: list[dict[str, str]] = self._call_sync("access_points", timeout=SCAN_CALL_TIMEOUT_SECONDS)
        return parse_access_points(records)

    def has_known_networks(self) -> bool:
        if self._known_in_memory:
            return True
        if self._known_network_marker is not None and self._known_network_marker.is_file():
            self._known_in_memory = True
            return True
        return False

    def connect(self, ssid: str, psk: str) -> None:
        state_str, name = self._observe_fresh_state()
        if state_str == "CONNECTED":
            logger.warning(
                "comitup: connect requested while CONNECTED to %r; forgetting it first "
                "(comitup would otherwise short-circuit back to it)",
                name,
            )
            # Deletes the old profile directly (same call forget_current
            # makes) rather than calling forget_current() itself, which
            # would poll state() again for no reason.
            self._call_sync("delete_connection", timeout=STATUS_CALL_TIMEOUT_SECONDS)
            self._clear_known()
        self._call_sync("connect", ssid, psk, timeout=STATUS_CALL_TIMEOUT_SECONDS)
        self._mark_known()

    def forget_current(self) -> None:
        state_str, name = self._observe_fresh_state()
        if state_str != "CONNECTED":
            raise NotConnectedError(f"comitup is not CONNECTED (observed {state_str!r}); nothing to forget")
        logger.info("comitup: forgetting current network %r", name)
        self._call_sync("delete_connection", timeout=STATUS_CALL_TIMEOUT_SECONDS)
        self._clear_known()

    def _observe_fresh_state(self) -> tuple[str, str]:
        """Poll comitup's ``state()`` fresh and log any transition -- never a cached value. See module docstring."""
        state_str, name = self._call_sync("state", timeout=STATUS_CALL_TIMEOUT_SECONDS)
        self._log_transition(state_str, name)
        return state_str, name

    def _log_transition(self, state: str, name: str) -> bool:
        """Log every state comitup is observed in: the first poll, and every change after it.

        comitup keeps no journal of its own state, so this is the only record of it.
        Returns ``True`` for the first observation this instance has ever made (else
        ``False``), passed to :meth:`_resolve_pending_attempt` -- see
        :mod:`palmimo_portal.core.wifi_attempt` for why.
        """
        with self._state_lock:
            current = (state, name)
            previous = self._last_logged
            self._last_logged = current
        if previous is None:
            logger.info("network state observed: %s (%s)", state, name)
            return True
        if previous != current:
            logger.info("network state: %s -> %s (%s)", previous[0], state, name)
        return False

    def _resolve_pending_attempt(self, state: str, name: str, *, is_first_observation: bool) -> None:
        """Resolve a pending ``last_wifi_attempt`` record once its outcome is observable.

        ``POST /wifi/connect`` records an attempt as ``"attempting"`` and returns
        immediately -- comitup's own attempt happens asynchronously, so without this a
        client reconnecting to the setup AP after a failure would see ``"attempting"``
        forever. Called on every observation, not only a detected change; delegates to
        :func:`~palmimo_portal.core.wifi_attempt.resolve_attempt` (see that module's
        docstring for why unconditional calling matters). No-op with no
        :attr:`_state_store` or nothing to resolve. A write failure is caught and
        logged at ERROR -- must never turn a successful status read into a failed one.
        """
        if self._state_store is None:
            return
        attempt = self._state_store.read_last_wifi_attempt()
        resolution = resolve_attempt(
            attempt=attempt,
            observed_state=map_comitup_state(state),
            is_first_observation=is_first_observation,
            observed_connection_name=name or None,
            now=time.time(),
        )
        if resolution is None:
            return
        assert attempt is not None  # resolve_attempt only returns non-None when attempt is not None
        logger.info(
            "resolving last_wifi_attempt %r -> %s (%s, observed connection %r)",
            attempt.ssid,
            resolution.result,
            resolution.reason,
            resolution.observed_connection_name,
        )
        try:
            self._state_store.write_last_wifi_attempt(
                WifiAttempt(
                    ssid=attempt.ssid,
                    result=resolution.result,
                    timestamp=time.time(),
                    observed_connection_name=resolution.observed_connection_name,
                )
            )
        except OSError as error:
            logger.error("failed to persist resolved last_wifi_attempt: %s", error)

    def _mark_known(self) -> None:
        with self._state_lock:
            if self._known_in_memory:
                return
            self._known_in_memory = True
        if self._known_network_marker is not None:
            try:
                self._known_network_marker.parent.mkdir(parents=True, exist_ok=True)
                self._known_network_marker.touch(exist_ok=True)
            except OSError as error:
                logger.error("failed to persist known-network marker %s: %s", self._known_network_marker, error)

    def _clear_known(self) -> None:
        """Clear the known-network marker after :meth:`forget_current` (or forget-before-connect) succeeds.

        The device is back in the out-of-box state, so
        :func:`~palmimo_portal.core.provisioning.is_provisioned` must report
        unprovisioned. If comitup reconnects to another known profile on its own, the
        next ``CONNECTED`` observation re-creates the marker via :meth:`_mark_known` --
        this can under-report "known" for at most one poll interval, never over-report.
        """
        with self._state_lock:
            self._known_in_memory = False
        if self._known_network_marker is not None:
            try:
                self._known_network_marker.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("failed to remove known-network marker %s: %s", self._known_network_marker, error)
