from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from dbus_fast import Variant
from dbus_fast.errors import DBusError

from palmimo_portal.adapters.systemd import SystemdWifiQrUnitPort
from palmimo_portal.ports import PolkitDeniedError, WifiQrUnitMissingError, WifiQrUnitStatus
from palmimo_portal.testing.fakes import FakeWifiQrUnitPort
from tests.test_wifi_qr_deadlines import Bus, port_for


@pytest.mark.parametrize("verb,member", [("start", "start_unit"), ("stop", "stop_unit"), ("status", "load_unit")])
def test_wifi_qr_unit_uses_fixed_name(verb: str, member: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if verb == "status":
        bus = Bus()
        port_for(bus, monkeypatch).status()
        assert "load" in bus.calls
        return
    port = SystemdWifiQrUnitPort()
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def call(method: str, *args: Any, **kwargs: Any) -> str:
        calls.append((method, args))
        return "/unit"

    monkeypatch.setattr(port._unit_port, "_call_sync", call)
    monkeypatch.setattr(port._unit_port, "_read_unit_properties", lambda *a, **kw: {})
    getattr(port, verb)()
    assert calls == [(member, ("palmimo-wifi-qr.service",) + (() if verb == "status" else ("replace",)))]


@pytest.mark.parametrize("verb", ["start", "stop", "status"])
def test_wifi_qr_unit_distinguishes_missing_unit(verb: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if verb == "status":
        bus = Bus()
        bus.error = "load", "org.freedesktop.systemd1.NoSuchUnit"
        with pytest.raises(WifiQrUnitMissingError):
            port_for(bus, monkeypatch).status()
        return
    port = SystemdWifiQrUnitPort()

    def call(*args: Any, **kwargs: Any) -> None:
        raise DBusError("org.freedesktop.systemd1.NoSuchUnit", "missing")

    monkeypatch.setattr(port._unit_port, "_call_sync", call)
    with pytest.raises(WifiQrUnitMissingError):
        getattr(port, verb)()


@pytest.mark.parametrize("verb", ["start", "stop", "status"])
@pytest.mark.parametrize(
    "error_name",
    [
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired",
    ],
)
def test_wifi_qr_unit_distinguishes_polkit_denial(
    verb: str,
    error_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = SystemdWifiQrUnitPort()
    method = "load_unit" if verb == "status" else f"{verb}_unit"
    interface = SimpleNamespace(**{f"call_{method}": AsyncMock(side_effect=DBusError(error_name, "denied"))})

    async def connect() -> tuple[Any, Any]:
        return interface, object()

    monkeypatch.setattr(port._unit_port, "_connect", connect)
    monkeypatch.setattr(port._unit_port, "_disconnect", AsyncMock())
    with pytest.raises(PolkitDeniedError):
        getattr(port, verb)()


def test_wifi_qr_unit_reads_exit_and_load_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = Bus()
    bus.state = "failed"
    bus.service_properties = {
        "Result": Variant("s", "exit-code"),
        "ExecMainCode": Variant("i", 1),
        "ExecMainStatus": Variant("i", 69),
    }
    assert port_for(bus, monkeypatch).status() == WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 69)


def test_wifi_qr_unit_distinguishes_not_found_load_state(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = Bus()
    bus.load_state = "not-found"
    with pytest.raises(WifiQrUnitMissingError):
        port_for(bus, monkeypatch).status()


def test_fake_wifi_qr_unit_supports_lifecycle_and_scripted_failures() -> None:
    port = FakeWifiQrUnitPort()
    port.start()
    assert port.status().active_state == "active"
    port.stop()
    assert port.status().active_state == "inactive"
    port.error = WifiQrUnitMissingError()
    with pytest.raises(WifiQrUnitMissingError):
        port.start()
