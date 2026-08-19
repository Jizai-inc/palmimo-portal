"""Real :class:`~palmimo_portal.ports.SystemPort`: power operations via ``logind``.

Talks to ``systemd-logind`` over the system bus:

- bus name ``org.freedesktop.login1``
- object path ``/org/freedesktop/login1``
- interface ``org.freedesktop.login1.Manager``
- ``Reboot(interactive: bool)`` / ``PowerOff(interactive: bool)`` -- both
  called with ``interactive=False``: the Portal is a headless web backend
  with no polkit authentication agent to prompt, so an interactive request
  would just fail rather than pop a dialog nobody can see.

Shares its D-Bus call plumbing (lazy-connect, reconnect-and-retry-once,
sync-over-async bridging via a background event loop) with
:class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort` -- see
:mod:`palmimo_portal.adapters.dbus_support` for why this bridge is needed at
all to drive dbus-fast's asyncio API from a synchronous Port method.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface

from palmimo_portal.adapters.dbus_support import SharedEventLoopThread, get_shared_loop_thread
from palmimo_portal.ports import AdapterUnavailableError, SystemPort


logger = logging.getLogger("palmimo_portal")

LOGIND_BUS_NAME = "org.freedesktop.login1"
LOGIND_OBJECT_PATH = "/org/freedesktop/login1"
LOGIND_INTERFACE = "org.freedesktop.login1.Manager"

#: The other D-Bus destination :meth:`SystemdSystemPort.restart_portal` talks
#: to -- systemd's own top-level manager (distinct from ``logind`` above),
#: which owns ``RestartUnit`` for an arbitrary unit like the Portal's own.
SYSTEMD_BUS_NAME = "org.freedesktop.systemd1"
SYSTEMD_OBJECT_PATH = "/org/freedesktop/systemd1"
SYSTEMD_INTERFACE = "org.freedesktop.systemd1.Manager"

DEFAULT_PORTAL_UNIT = "palmimo-portal.service"

CALL_TIMEOUT_SECONDS = 5.0

#: Bounds :meth:`SystemdSystemPort._connect`'s bus-open sequence, independent
#: of the eventual RPC call's own timeout -- mirrors
#: :data:`palmimo_portal.adapters.comitup.CONNECT_TIMEOUT_SECONDS`.
CONNECT_TIMEOUT_SECONDS = 5.0

_ADAPTER_ERROR_CODE = "system_backend_unavailable"


class SystemdSystemPort(SystemPort):
    """Reboots/shuts down the machine via ``logind``'s D-Bus ``Manager`` interface.

    Mirrors :class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort`'s
    ``_call`` / ``_call_resilient`` / ``_call_sync`` structure and concurrency
    guarantees -- see that class's docstring for what each layer does and how
    tests stub ``_call``/``_open_bus`` without a real bus.
    """

    def __init__(self, *, loop_thread: SharedEventLoopThread | None = None, unit: str = DEFAULT_PORTAL_UNIT) -> None:
        self._loop_thread = loop_thread if loop_thread is not None else get_shared_loop_thread()
        self._unit = unit
        self._bus: MessageBus | None = None
        self._interface: ProxyInterface | None = None
        # Second, independent connection to systemd1's own Manager interface
        # (distinct from logind above), used only by restart_portal().
        # Shares _lock with the logind pair -- both are small, infrequent
        # operations, so one lock is simpler and the contention immaterial.
        self._systemd_bus: MessageBus | None = None
        self._systemd_interface: ProxyInterface | None = None
        self._lock = asyncio.Lock()

    async def _open_bus(self) -> tuple[MessageBus, ProxyInterface]:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            introspection = await bus.introspect(LOGIND_BUS_NAME, LOGIND_OBJECT_PATH)
            proxy_object = bus.get_proxy_object(LOGIND_BUS_NAME, LOGIND_OBJECT_PATH, introspection)
            interface = proxy_object.get_interface(LOGIND_INTERFACE)
        except BaseException:
            # BaseException, not Exception: must also run on
            # asyncio.CancelledError, which _connect()'s asyncio.wait_for
            # raises here once CONNECT_TIMEOUT_SECONDS elapses -- otherwise
            # a bus hung in introspect() past the timeout is never
            # disconnected. See ComitupNetworkPort._open_bus.
            try:
                bus.disconnect()
            except Exception:
                logger.debug("logind: error disconnecting a bus that failed to initialize (ignored)", exc_info=True)
            raise
        return bus, interface

    async def _connect(self) -> tuple[ProxyInterface, MessageBus]:
        async with self._lock:
            if self._interface is not None and self._bus is not None:
                return self._interface, self._bus
            bus, interface = await asyncio.wait_for(self._open_bus(), timeout=CONNECT_TIMEOUT_SECONDS)
            self._bus, self._interface = bus, interface
            return interface, bus

    async def _disconnect(self, bus: MessageBus) -> None:
        async with self._lock:
            if self._bus is not bus:
                return
            self._bus = None
            self._interface = None
        try:
            bus.disconnect()
        except Exception:
            logger.debug("logind: error disconnecting a stale bus connection (ignored)", exc_info=True)

    async def _open_systemd_bus(self) -> tuple[MessageBus, ProxyInterface]:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            introspection = await bus.introspect(SYSTEMD_BUS_NAME, SYSTEMD_OBJECT_PATH)
            proxy_object = bus.get_proxy_object(SYSTEMD_BUS_NAME, SYSTEMD_OBJECT_PATH, introspection)
            interface = proxy_object.get_interface(SYSTEMD_INTERFACE)
        except BaseException:
            # Same CancelledError reasoning as _open_bus above.
            try:
                bus.disconnect()
            except Exception:
                logger.debug("systemd1: error disconnecting a bus that failed to initialize (ignored)", exc_info=True)
            raise
        return bus, interface

    async def _connect_systemd(self) -> tuple[ProxyInterface, MessageBus]:
        async with self._lock:
            if self._systemd_interface is not None and self._systemd_bus is not None:
                return self._systemd_interface, self._systemd_bus
            bus, interface = await asyncio.wait_for(self._open_systemd_bus(), timeout=CONNECT_TIMEOUT_SECONDS)
            self._systemd_bus, self._systemd_interface = bus, interface
            return interface, bus

    async def _disconnect_systemd(self, bus: MessageBus) -> None:
        async with self._lock:
            if self._systemd_bus is not bus:
                return
            self._systemd_bus = None
            self._systemd_interface = None
        try:
            bus.disconnect()
        except Exception:
            logger.debug("systemd1: error disconnecting a stale bus connection (ignored)", exc_info=True)

    async def _call(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
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
        try:
            return await self._call(member, args, timeout)
        except TimeoutError as error:
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"logind {member}() timed out") from error
        except Exception as first_error:
            logger.warning("logind D-Bus call %r failed, reconnecting and retrying once: %s", member, first_error)
            try:
                return await self._call(member, args, timeout)
            except Exception as second_error:
                raise AdapterUnavailableError(
                    _ADAPTER_ERROR_CODE, f"logind {member}() failed after a reconnect attempt: {second_error}"
                ) from second_error

    def _call_sync(self, member: str, *args: Any, timeout: float) -> Any:
        try:
            return self._loop_thread.run(self._call_resilient(member, args, timeout), timeout=timeout + 1.0)
        except AdapterUnavailableError:
            raise
        except Exception as error:
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"logind {member}() failed: {error}") from error

    async def _call_systemd(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        interface, bus = await self._connect_systemd()
        method = getattr(interface, f"call_{member}")
        try:
            return await asyncio.wait_for(method(*args), timeout=timeout)
        except TimeoutError:
            raise
        except Exception:
            await self._disconnect_systemd(bus)
            raise

    async def _call_systemd_resilient(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        try:
            return await self._call_systemd(member, args, timeout)
        except TimeoutError as error:
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"systemd1 {member}() timed out") from error
        except Exception as first_error:
            logger.warning("systemd1 D-Bus call %r failed, reconnecting and retrying once: %s", member, first_error)
            try:
                return await self._call_systemd(member, args, timeout)
            except Exception as second_error:
                raise AdapterUnavailableError(
                    _ADAPTER_ERROR_CODE, f"systemd1 {member}() failed after a reconnect attempt: {second_error}"
                ) from second_error

    def _call_systemd_sync(self, member: str, *args: Any, timeout: float) -> Any:
        try:
            return self._loop_thread.run(self._call_systemd_resilient(member, args, timeout), timeout=timeout + 1.0)
        except AdapterUnavailableError:
            raise
        except Exception as error:
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"systemd1 {member}() failed: {error}") from error

    def reboot(self) -> None:
        self._call_sync("reboot", False, timeout=CALL_TIMEOUT_SECONDS)

    def shutdown(self) -> None:
        self._call_sync("power_off", False, timeout=CALL_TIMEOUT_SECONDS)

    def restart_portal(self) -> None:
        """Restart this Portal's own systemd unit via ``systemd1``'s ``Manager.RestartUnit``.

        ``mode="replace"`` -- standard systemctl-restart semantics (queue
        the restart, replacing any queued job for the same unit).
        """
        self._call_systemd_sync("restart_unit", self._unit, "replace", timeout=CALL_TIMEOUT_SECONDS)
