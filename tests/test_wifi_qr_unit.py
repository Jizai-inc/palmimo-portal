from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from dbus_fast.errors import DBusError

from palmimo_portal.adapters.systemd import SystemdWifiQrUnitPort
from palmimo_portal.ports import PolkitDeniedError, WifiQrUnitMissingError, WifiQrUnitStatus
from palmimo_portal.testing.fakes import FakeWifiQrUnitPort


@pytest.mark.parametrize("verb,member", [("start", "start_unit"), ("stop", "stop_unit"), ("status", "load_unit")])
def test_wifi_qr_unit_uses_fixed_name(verb: str, member: str, monkeypatch: pytest.MonkeyPatch) -> None:
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
    port = SystemdWifiQrUnitPort()
    monkeypatch.setattr(port._unit_port, "_call_sync", lambda *a, **kw: "/unit")
    monkeypatch.setattr(
        port._unit_port,
        "_read_unit_properties",
        lambda *a, **kw: {
            "ActiveState": "failed",
            "LoadState": "loaded",
            "Result": "exit-code",
            "ExecMainCode": 1,
            "ExecMainStatus": 69,
        },
    )
    assert port.status() == WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 69)


def test_wifi_qr_unit_distinguishes_not_found_load_state(monkeypatch: pytest.MonkeyPatch) -> None:
    port = SystemdWifiQrUnitPort()
    monkeypatch.setattr(port._unit_port, "_call_sync", lambda *a, **kw: "/unit")
    monkeypatch.setattr(port._unit_port, "_read_unit_properties", lambda *a, **kw: {"LoadState": "not-found"})
    with pytest.raises(WifiQrUnitMissingError):
        port.status()


def test_fake_wifi_qr_unit_supports_lifecycle_and_scripted_failures() -> None:
    port = FakeWifiQrUnitPort()
    port.start()
    assert port.status().active_state == "active"
    port.stop()
    assert port.status().active_state == "inactive"
    port.error = WifiQrUnitMissingError()
    with pytest.raises(WifiQrUnitMissingError):
        port.start()
