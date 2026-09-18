"""Real :class:`~palmimo_portal.ports.SystemPort`: power operations via ``logind`` D-Bus.

``Reboot(interactive=False)`` / ``PowerOff(interactive=False)``: the Portal
is a headless web backend with no polkit authentication agent to prompt, so
an interactive request would just fail rather than pop a dialog nobody can
see. Shares D-Bus call plumbing (lazy-connect, reconnect-and-retry-once,
sync-over-async bridging) with
:class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort` -- see
:mod:`palmimo_portal.adapters.dbus_support`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface
from dbus_fast.errors import DBusError

from palmimo_portal.adapters.dbus_support import SharedEventLoopThread, get_shared_loop_thread
from palmimo_portal.ports import (
    AdapterUnavailableError,
    AppUnitPort,
    PolkitDeniedError,
    SyncUnitPort,
    SystemPort,
    UnitStatus,
)


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
    ``_call``/``_call_resilient``/``_call_sync`` structure and concurrency
    guarantees.
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


#: D-Bus error names polkit raises for a refused verb (design doc 2.6): AccessDenied is the
#: outright refusal, InteractiveAuthorizationRequired the "would need a prompt" case -- there is
#: no polkit agent on a headless Portal, so both mean the same thing here: refused.
_POLKIT_DENIED_DBUS_ERRORS = frozenset(
    {"org.freedesktop.DBus.Error.AccessDenied", "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired"}
)

_APP_UNIT_TEMPLATE = "palmimo-app@{name}.service"
_APP_UNIT_PROPERTIES_INTERFACE = "org.freedesktop.systemd1.Unit"
#: `Result`/`ExecMainStatus`/`ExecMainStartTimestamp` live on the Service interface, not Unit.
_APP_UNIT_SERVICE_PROPERTIES_INTERFACE = "org.freedesktop.systemd1.Service"
_UNIT_START_STATES = frozenset({"active", "activating", "deactivating"})

#: `LoadUnit` on a template instance systemd has since garbage-collected (every stopped app,
#: eventually) raises one of these -- not a backend failure, just "never started / not running".
_UNIT_NOT_LOADED_DBUS_ERRORS = frozenset({"org.freedesktop.systemd1.NoSuchUnit", "org.freedesktop.systemd1.NoSuchFile"})


def app_unit_name(name: str) -> str:
    """Return the ``palmimo-app@<name>.service`` unit string for app ``name``."""
    return _APP_UNIT_TEMPLATE.format(name=name)


class SystemdAppUnitPort(AppUnitPort):
    """Controls ``palmimo-app@<name>.service`` units via systemd1's D-Bus ``Manager`` interface.

    A separate connection from :class:`SystemdSystemPort`'s (rather than a
    shared one): app-start/stop calls run at a very different cadence than
    an occasional reboot/shutdown, and sharing one lock would make an app
    start wait behind an unrelated power operation for no benefit.
    """

    def __init__(self, *, loop_thread: SharedEventLoopThread | None = None) -> None:
        self._loop_thread = loop_thread if loop_thread is not None else get_shared_loop_thread()
        self._bus: MessageBus | None = None
        self._interface: ProxyInterface | None = None
        self._lock = asyncio.Lock()

    async def _open_bus(self) -> tuple[MessageBus, ProxyInterface]:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            introspection = await bus.introspect(SYSTEMD_BUS_NAME, SYSTEMD_OBJECT_PATH)
            proxy_object = bus.get_proxy_object(SYSTEMD_BUS_NAME, SYSTEMD_OBJECT_PATH, introspection)
            interface = proxy_object.get_interface(SYSTEMD_INTERFACE)
        except BaseException:
            try:
                bus.disconnect()
            except Exception:
                logger.debug("systemd1: error disconnecting a bus that failed to initialize (ignored)", exc_info=True)
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
            logger.debug("systemd1: error disconnecting a stale bus connection (ignored)", exc_info=True)

    async def _call(
        self,
        member: str,
        args: tuple[Any, ...],
        *,
        unit: str,
        verb: str,
        reraise_dbus_errors: frozenset[str] = frozenset(),
    ) -> Any:
        interface, bus = await self._connect()
        method = getattr(interface, f"call_{member}")
        try:
            return await asyncio.wait_for(method(*args), timeout=CALL_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"systemd1 {member}() timed out") from error
        except DBusError as error:
            await self._disconnect(bus)
            if error.type in reraise_dbus_errors:
                raise
            if error.type in _POLKIT_DENIED_DBUS_ERRORS:
                logger.error("systemd: polkit denied unit=%s verb=%s", unit, verb)
                raise PolkitDeniedError(unit, verb) from error
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"systemd1 {member}() failed: {error}") from error
        except Exception as error:
            await self._disconnect(bus)
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"systemd1 {member}() failed: {error}") from error

    def _call_sync(
        self, member: str, *args: Any, unit: str, verb: str, reraise_dbus_errors: frozenset[str] = frozenset()
    ) -> Any:
        return self._loop_thread.run(
            self._call(member, args, unit=unit, verb=verb, reraise_dbus_errors=reraise_dbus_errors),
            timeout=CALL_TIMEOUT_SECONDS + 1.0,
        )

    def set_device_allow(self, name: str, specs: list[str]) -> None:
        unit = app_unit_name(name)
        self._call_sync(
            "set_unit_properties", unit, True, [("DeviceAllow", Variant("a(ss)", []))], unit=unit, verb="set-property"
        )
        if specs:
            self._call_sync(
                "set_unit_properties",
                unit,
                True,
                [("DeviceAllow", Variant("a(ss)", [(spec, "rw") for spec in specs]))],
                unit=unit,
                verb="set-property",
            )

    def start(self, name: str) -> None:
        self._start_unit(app_unit_name(name))

    def stop(self, name: str) -> None:
        self._stop_unit(app_unit_name(name))

    def status(self, name: str) -> UnitStatus:
        return self._status_for_unit(app_unit_name(name))

    def _start_unit(self, unit: str) -> None:
        self._call_sync("start_unit", unit, "replace", unit=unit, verb="start")

    def _stop_unit(self, unit: str) -> None:
        self._call_sync("stop_unit", unit, "replace", unit=unit, verb="stop")

    def _status_for_unit(self, unit: str) -> UnitStatus:
        # `LoadUnit` (not `GetUnit`): systemd garbage-collects an inactive template instance's
        # in-memory unit object, so `GetUnit` on a stopped app raises `NoSuchUnit` even though the
        # unit is legitimately just "never started" rather than unreachable.
        try:
            unit_path = self._call_sync(
                "load_unit", unit, unit=unit, verb="status", reraise_dbus_errors=_UNIT_NOT_LOADED_DBUS_ERRORS
            )
        except DBusError as error:
            if error.type in _UNIT_NOT_LOADED_DBUS_ERRORS:
                return UnitStatus(
                    active_state="inactive",
                    sub_state="dead",
                    result="success",
                    exec_main_status=0,
                    exec_main_start_timestamp=0.0,
                )
            raise
        properties = self._read_unit_properties(unit_path, unit=unit)
        return UnitStatus(
            active_state=str(properties.get("ActiveState", "unknown")),
            sub_state=str(properties.get("SubState", "unknown")),
            result=str(properties.get("Result", "success")),
            exec_main_status=int(properties.get("ExecMainStatus", 0)),
            exec_main_start_timestamp=float(properties.get("ExecMainStartTimestamp", 0)),
        )

    def _read_unit_properties(self, unit_path: str, *, unit: str) -> dict[str, Any]:
        async def _get_all() -> dict[str, Any]:
            _interface, bus = await self._connect()
            introspection = await bus.introspect(SYSTEMD_BUS_NAME, unit_path)
            proxy_object = bus.get_proxy_object(SYSTEMD_BUS_NAME, unit_path, introspection)
            properties_interface = proxy_object.get_interface("org.freedesktop.DBus.Properties")
            # dbus_fast generates call_* methods from introspection at runtime; ProxyInterface's
            # static type has no attribute for one specific to this generated interface.
            get_all = properties_interface.call_get_all  # type: ignore[attr-defined]
            unit_props = await asyncio.wait_for(get_all(_APP_UNIT_PROPERTIES_INTERFACE), timeout=CALL_TIMEOUT_SECONDS)
            service_props = await asyncio.wait_for(
                get_all(_APP_UNIT_SERVICE_PROPERTIES_INTERFACE), timeout=CALL_TIMEOUT_SECONDS
            )
            merged = {**unit_props, **service_props}
            return {key: (value.value if isinstance(value, Variant) else value) for key, value in merged.items()}

        try:
            return self._loop_thread.run(_get_all(), timeout=CALL_TIMEOUT_SECONDS + 1.0)
        except Exception as error:
            raise AdapterUnavailableError(
                _ADAPTER_ERROR_CODE, f"systemd1 unit properties read failed: {error}"
            ) from error

    def list_active_app_units(self) -> list[str]:
        return self._list_active_units("palmimo-app@")

    def _list_active_units(self, unit_prefix: str) -> list[str]:
        units = self._call_sync("list_units", unit="*", verb="status")
        active_names = []
        for entry in units:
            unit_name = str(entry[0])
            active_state = str(entry[3])
            if unit_name.startswith(unit_prefix) and active_state in _UNIT_START_STATES:
                active_names.append(unit_name.removeprefix(unit_prefix).removesuffix(".service"))
        return sorted(active_names)


_SYNC_UNIT_TEMPLATE = "palmimo-app-sync@{name}.service"

#: How often `SystemdSyncUnitPort.wait` re-reads the unit's status while it is still running.
_SYNC_POLL_INTERVAL_SECONDS = 0.5


def sync_unit_name(instance: str) -> str:
    """Return the ``palmimo-app-sync@<instance>.service`` unit string for a staging instance."""
    return _SYNC_UNIT_TEMPLATE.format(name=instance)


class SystemdSyncUnitPort(SyncUnitPort):
    """Controls one job's ``palmimo-app-sync@<instance>.service`` oneshot unit (design doc 2.2b).

    A sibling of :class:`SystemdAppUnitPort`, reusing its D-Bus connection/call plumbing
    (``_start_unit``/``_stop_unit``/``_status_for_unit``) rather than duplicating it -- only
    the unit-name pattern and the blocking :meth:`wait` differ.
    """

    def __init__(self, *, loop_thread: SharedEventLoopThread | None = None) -> None:
        self._unit_port = SystemdAppUnitPort(loop_thread=loop_thread)

    def start(self, instance: str) -> None:
        self._unit_port._start_unit(sync_unit_name(instance))

    def stop(self, instance: str) -> None:
        self._unit_port._stop_unit(sync_unit_name(instance))

    def list_active_instances(self) -> list[str]:
        return self._unit_port._list_active_units("palmimo-app-sync@")

    def wait(self, instance: str, timeout_s: float) -> UnitStatus:
        unit = sync_unit_name(instance)
        deadline = time.monotonic() + timeout_s
        status = self._unit_port._status_for_unit(unit)
        while status.active_state in _UNIT_START_STATES and time.monotonic() < deadline:
            time.sleep(_SYNC_POLL_INTERVAL_SECONDS)
            status = self._unit_port._status_for_unit(unit)
        if status.active_state in _UNIT_START_STATES:
            raise TimeoutError(f"sync unit {instance} did not finish within {timeout_s:g}s")
        return status
