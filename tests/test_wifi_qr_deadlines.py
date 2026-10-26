import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from dbus_fast import Variant
from dbus_fast.errors import DBusError

from palmimo_portal.adapters.systemd import SystemdWifiQrUnitPort
from palmimo_portal.ports import PolkitDeniedError, WifiQrUnitMissingError


class Bus:
    def __init__(self) -> None:
        self.delay: dict[str, float] = {}
        self.block: str | None = None
        self.cancelled = threading.Event()
        self.entered = threading.Event()
        self.unblock_on_cancel = False
        self.calls: list[str] = []
        self.state = "active"
        self.load_state = "loaded"
        self.service_properties = {
            "Result": Variant("s", "success"),
            "ExecMainCode": Variant("i", 1),
            "ExecMainStatus": Variant("i", 0),
        }
        self.error: tuple[str, str] | None = None
        self.interface = SimpleNamespace(
            call_load_unit=self.load,
            call_stop_unit=self.stop,
            call_start_unit=self.start,
        )

    async def checkpoint(self, stage: str) -> None:
        self.calls.append(stage)
        if self.error is not None and self.error[0] == stage:
            raise DBusError(self.error[1], "refused")
        if self.block == stage:
            self.entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                if self.unblock_on_cancel:
                    self.block = None
                self.cancelled.set()
        await asyncio.sleep(self.delay.get(stage, 0))

    async def load(self, unit: str) -> str:
        assert unit == "palmimo-wifi-qr.service"
        await self.checkpoint("load")
        return "/unit"

    async def stop(self, unit: str, mode: str) -> str:
        assert (unit, mode) == ("palmimo-wifi-qr.service", "replace")
        await self.checkpoint("stop")
        self.state = "inactive"
        return "/job"

    async def start(self, unit: str, mode: str) -> str:
        assert (unit, mode) == ("palmimo-wifi-qr.service", "replace")
        await self.checkpoint("start")
        self.state = "active"
        return "/job"

    async def introspect(self, *args: Any) -> object:
        await self.checkpoint("introspect")
        return object()

    def get_proxy_object(self, *args: Any) -> Any:
        return SimpleNamespace(get_interface=lambda name: SimpleNamespace(call_get_all=self.properties))

    async def properties(self, interface: str) -> dict[str, Variant]:
        if interface.endswith(".Unit"):
            await self.checkpoint("unit")
            return {"LoadState": Variant("s", self.load_state), "ActiveState": Variant("s", self.state)}
        await self.checkpoint("service")
        return self.service_properties


def port_for(bus: Bus, monkeypatch: pytest.MonkeyPatch) -> SystemdWifiQrUnitPort:
    port = SystemdWifiQrUnitPort(request_timeout_s=0.1)

    async def connect() -> tuple[Any, Any]:
        await bus.checkpoint("connect")
        return bus.interface, bus

    monkeypatch.setattr(port._unit_port, "_connect", connect)
    monkeypatch.setattr(port._unit_port, "_disconnect", AsyncMock())
    return port


def test_reader_stop_uses_one_deadline_across_request_and_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = Bus()
    bus.delay = {"stop": 0.04, "service": 0.08}
    port = port_for(bus, monkeypatch)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        port.stop_and_wait(timeout_s=0.07)
    assert time.monotonic() - started < 0.3


@pytest.mark.parametrize("stage", ["connect", "stop", "load", "introspect", "unit", "service"])
def test_reader_deadline_cancels_inflight_operation_without_late_stop(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = Bus()
    bus.block = stage
    port = port_for(bus, monkeypatch)
    with pytest.raises(TimeoutError):
        port.stop_and_wait(timeout_s=0.03)
    assert bus.cancelled.wait(1)
    calls = list(bus.calls)
    bus.block = None
    port.start()
    assert bus.state == "active"
    assert bus.calls[len(calls) :] == ["connect", "start"]


def test_reader_status_has_a_single_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = Bus()
    bus.block = "service"
    port = port_for(bus, monkeypatch)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        port.status()
    assert time.monotonic() - started < 0.4
    assert bus.cancelled.wait(1)


@pytest.mark.parametrize("stage", ["unit", "service"])
@pytest.mark.parametrize(
    "error_name,expected",
    [
        ("org.freedesktop.systemd1.NoSuchUnit", WifiQrUnitMissingError),
        ("org.freedesktop.DBus.Error.AccessDenied", PolkitDeniedError),
        ("org.freedesktop.DBus.Error.InteractiveAuthorizationRequired", PolkitDeniedError),
    ],
)
def test_reader_bounded_status_preserves_error_contract(
    stage: str,
    error_name: str,
    expected: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = Bus()
    bus.error = stage, error_name
    port = port_for(bus, monkeypatch)
    with pytest.raises(expected):
        port.status()


@pytest.mark.parametrize(
    "error_name,expected",
    [
        ("org.freedesktop.systemd1.NoSuchUnit", WifiQrUnitMissingError),
        ("org.freedesktop.DBus.Error.AccessDenied", PolkitDeniedError),
        ("org.freedesktop.DBus.Error.InteractiveAuthorizationRequired", PolkitDeniedError),
    ],
)
def test_reader_bounded_stop_preserves_error_contract(
    error_name: str,
    expected: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = Bus()
    bus.error = "stop", error_name
    port = port_for(bus, monkeypatch)
    with pytest.raises(expected):
        port.stop_and_wait(timeout_s=0.2)


@pytest.mark.parametrize("terminal", ["inactive", "failed"])
def test_reader_stop_waits_until_terminal_state(terminal: str, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = Bus()
    port = port_for(bus, monkeypatch)
    original_stop = bus.interface.call_stop_unit
    original_properties = bus.properties

    async def stop(unit: str, mode: str) -> str:
        result = await original_stop(unit, mode)
        bus.state = "deactivating"
        return result

    async def properties(interface: str) -> dict[str, Variant]:
        if bus.calls.count("unit") >= 2:
            bus.state = terminal
        return await original_properties(interface)

    bus.interface.call_stop_unit = stop
    monkeypatch.setattr(bus, "properties", properties)
    port.stop_and_wait(timeout_s=0.8)
    assert bus.state == terminal


def test_reader_deadline_includes_time_waiting_for_shared_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = Bus()
    port = port_for(bus, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    async def hold_loop() -> None:
        entered.set()
        assert release.wait(2)

    loop_thread = port._unit_port._loop_thread
    blocker = asyncio.run_coroutine_threadsafe(hold_loop(), loop_thread._loop)
    try:
        assert entered.wait(1)
        with pytest.raises(TimeoutError):
            port.stop_and_wait(timeout_s=0.03)
    finally:
        release.set()
        blocker.result(timeout=1)
    loop_thread.run(asyncio.sleep(0), timeout=1)
    assert bus.calls == []
    port.start()
    assert bus.state == "active"
