"""Real :class:`~palmimo_portal.ports.ClockPort`: ``time.monotonic`` plus ``timedate1`` over D-Bus.

Shares the connect/reconnect/sync-over-async plumbing
:mod:`palmimo_portal.adapters.dbus_support` gives every D-Bus adapter.
Unlike :class:`~palmimo_portal.adapters.systemd.SystemdSystemPort`, a
failed D-Bus call here never raises -- :meth:`SystemClockPort.ntp_synchronized`
fails closed (``False``), logged once, since every caller (the periodic
scheduler) treats "unknown" and "unsynchronized" identically: skip network
work rather than risk it on a clock that might be years off.
"""

from __future__ import annotations

import asyncio
import logging
import time

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface

from palmimo_portal.adapters.dbus_support import SharedEventLoopThread, get_shared_loop_thread
from palmimo_portal.ports import ClockPort


logger = logging.getLogger("palmimo_portal")

TIMEDATE_BUS_NAME = "org.freedesktop.timedate1"
TIMEDATE_OBJECT_PATH = "/org/freedesktop/timedate1"
TIMEDATE_INTERFACE = "org.freedesktop.timedate1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

CALL_TIMEOUT_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 5.0

#: How long a read of NTPSynchronized is reused before asking timedate1 again -- the periodic
#: scheduler and several request paths all read this every tick; a D-Bus round trip per read
#: is unnecessary for a value that only ever flips once, shortly after boot.
NTP_SYNC_CACHE_SECONDS = 30.0


class SystemClockPort(ClockPort):
    def __init__(self, *, loop_thread: SharedEventLoopThread | None = None) -> None:
        self._loop_thread = loop_thread if loop_thread is not None else get_shared_loop_thread()
        self._bus: MessageBus | None = None
        self._interface: ProxyInterface | None = None
        self._lock = asyncio.Lock()
        self._warned = False
        self._ntp_cache: tuple[float, bool] | None = None

    def monotonic(self) -> float:
        return time.monotonic()

    async def _open_bus(self) -> tuple[MessageBus, ProxyInterface]:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            introspection = await bus.introspect(TIMEDATE_BUS_NAME, TIMEDATE_OBJECT_PATH)
            proxy_object = bus.get_proxy_object(TIMEDATE_BUS_NAME, TIMEDATE_OBJECT_PATH, introspection)
            interface = proxy_object.get_interface(PROPERTIES_INTERFACE)
        except BaseException:
            try:
                bus.disconnect()
            except Exception:
                logger.debug("timedate1: error disconnecting a bus that failed to initialize (ignored)", exc_info=True)
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
            logger.debug("timedate1: error disconnecting a stale bus connection (ignored)", exc_info=True)

    async def _read_ntp_synchronized(self) -> bool:
        interface, bus = await self._connect()
        try:
            # dbus_fast generates call_* methods from introspection at runtime; ProxyInterface's
            # static type has no attribute for one specific to this generated interface.
            get = interface.call_get  # type: ignore[attr-defined]
            variant = await asyncio.wait_for(get(TIMEDATE_INTERFACE, "NTPSynchronized"), timeout=CALL_TIMEOUT_SECONDS)
            return bool(variant.value)
        except Exception:
            await self._disconnect(bus)
            raise

    def ntp_synchronized(self) -> bool:
        now = time.monotonic()
        if self._ntp_cache is not None and now - self._ntp_cache[0] < NTP_SYNC_CACHE_SECONDS:
            return self._ntp_cache[1]
        try:
            result = self._loop_thread.run(self._read_ntp_synchronized(), timeout=CALL_TIMEOUT_SECONDS + 1.0)
        except Exception as error:
            if not self._warned:
                logger.warning("timedate1: could not read NTPSynchronized, treating as unsynchronized: %s", error)
                self._warned = True
            result = False
        self._ntp_cache = (now, result)
        return result
