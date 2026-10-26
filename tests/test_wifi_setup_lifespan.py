import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api.app import _lifespan
from palmimo_portal.core.wifi_setup import WifiSetup
from palmimo_portal.ports import (
    AdapterUnavailableError,
    AppRecord,
    AppSource,
    ConnectionState,
    PolkitDeniedError,
    PreemptedApp,
    UnitStatus,
    WifiDeviceStatus,
    WifiQrUnitMissingError,
    WifiQrUnitStatus,
    WifiStatus,
)
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return replace(settings, apps_dir=tmp_path / "apps")


def install_app(app: FastAPI, name: str, *, autostart: bool, devices: str = '"camera"') -> None:
    path: Path = app.state.apps_job_context.app_dir(name)
    (path / ".venv" / "bin").mkdir(parents=True)
    (path / ".venv" / "bin" / "python").touch()
    (path / "palmimo.toml").write_text(
        f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\ndevices = [{devices}]\n'
    )
    state = app.state.adapters.state
    record = AppRecord(
        name=name,
        id=name,
        source=AppSource(type="zip"),
        installed_at=0,
        params={},
        autostart=autostart,
        last_job=None,
    )
    state.update_apps_state(lambda current: replace(current, apps={**current.apps, name: record}))


@pytest.mark.parametrize("by", ["wifi-setup", "display-firmware"])
@pytest.mark.parametrize("network", list(ConnectionState))
def test_startup_recovery_owner_controls_restore_and_suppresses_autostart(
    app: FastAPI,
    adapters: FakeAdapterBundle,
    by: str,
    network: ConnectionState,
) -> None:
    install_app(app, "auto", autostart=True)
    install_app(app, "face", autostart=False)
    record = PreemptedApp("face", by)
    adapters.state.write_preempted_app(record)
    adapters.network.status = WifiStatus(network, "home", "192.0.2.1" if network is ConnectionState.CONNECTED else None)
    adapters.wifi_profiles.device = WifiDeviceStatus(100, None)
    restored = by == "display-firmware" or network is ConnectionState.CONNECTED
    with TestClient(app):
        assert adapters.app_unit.start_calls == (["face"] if restored else [])
        assert adapters.state.read_preempted_app() == (None if restored else record)


def test_startup_retains_wifi_record_until_nm_is_activated(app: FastAPI, adapters: FakeAdapterBundle) -> None:
    install_app(app, "auto", autostart=True)
    install_app(app, "face", autostart=False)
    record = PreemptedApp("face", "wifi-setup")
    adapters.state.write_preempted_app(record)
    adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    adapters.wifi_profiles.device = WifiDeviceStatus(70, None)
    with TestClient(app):
        assert adapters.app_unit.start_calls == []
        assert adapters.state.read_preempted_app() == record


@pytest.mark.parametrize(
    "failure",
    [
        WifiQrUnitMissingError(),
        PolkitDeniedError("palmimo-wifi-qr.service", "stop"),
        RuntimeError("no bus"),
    ],
)
def test_startup_reader_stop_failure_does_not_block_autostart(
    app: FastAPI,
    adapters: FakeAdapterBundle,
    failure: Exception,
) -> None:
    install_app(app, "auto", autostart=True, devices='"audio"')
    adapters.wifi_qr_unit.error = failure
    with TestClient(app):
        assert adapters.app_unit.start_calls == ["auto"]


def test_startup_waits_for_reader_stop_before_autostart(
    app: FastAPI,
    adapters: FakeAdapterBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_app(app, "auto", autostart=True, devices='"audio"')
    checks: list[bool] = []

    def status() -> WifiQrUnitStatus:
        ready = len(checks) >= 2
        checks.append(ready)
        return WifiQrUnitStatus("inactive" if ready else "deactivating")

    monkeypatch.setattr(adapters.wifi_qr_unit, "status", status)
    original_start = adapters.app_unit.start

    def start(name: str) -> None:
        assert checks[-1] is True
        assert "stop" in adapters.wifi_qr_unit.calls
        original_start(name)

    monkeypatch.setattr(adapters.app_unit, "start", start)
    with TestClient(app):
        assert adapters.app_unit.start_calls == ["auto"]


def test_lifespan_starts_and_joins_setup_monitor(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = threading.Event()
    monkeypatch.setattr(WifiSetup, "tick", lambda self: observed.set())
    app.state.settings = replace(app.state.settings, wifi_setup_enabled=True)
    with TestClient(app):
        assert observed.wait(2)
        monitors = [thread for thread in threading.enumerate() if thread.name == "palmimo-wifi-setup"]
        assert len(monitors) == 1
    assert not monitors[0].is_alive()


def test_autostart_remains_preemptible_in_ap_mode(app: FastAPI, adapters: FakeAdapterBundle) -> None:
    install_app(app, "face", autostart=True)
    with TestClient(app):
        assert adapters.app_unit.start_calls == ["face"]
        assert adapters.state.read_preempted_app() is None
        app.state.wifi_setup.tick()
        for _ in range(3):
            adapters.clock.monotonic_value += 2
            app.state.wifi_setup.tick()
        assert adapters.app_unit.stop_calls == ["face"]
        assert adapters.state.read_preempted_app() == PreemptedApp("face", "wifi-setup")


@pytest.mark.parametrize("active", ["active", "activating", "deactivating"])
def test_startup_does_not_restart_recorded_live_unit(
    app: FastAPI,
    adapters: FakeAdapterBundle,
    active: str,
) -> None:
    install_app(app, "face", autostart=False)
    adapters.state.write_preempted_app(PreemptedApp("face", "wifi-setup"))
    adapters.app_unit.simulate_active_state("face", UnitStatus(active, "", "success", 0))
    adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    adapters.wifi_profiles.device = WifiDeviceStatus(100, None)
    with TestClient(app):
        assert adapters.app_unit.start_calls == []
        assert adapters.app_unit.device_allow_calls == []
        assert adapters.state.read_preempted_app() is None


def test_startup_defers_recovery_when_network_observation_fails(
    app: FastAPI,
    adapters: FakeAdapterBundle,
) -> None:
    install_app(app, "auto", autostart=True)
    install_app(app, "face", autostart=False)
    record = PreemptedApp("face", "wifi-setup")
    adapters.state.write_preempted_app(record)
    adapters.network.raise_on_get_status = AdapterUnavailableError("offline", "D-Bus unavailable")
    with TestClient(app):
        assert adapters.app_unit.start_calls == []
        assert adapters.state.read_preempted_app() == record


async def test_lifespan_joins_monitor_when_context_exits_with_error(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = threading.Event()
    monkeypatch.setattr(WifiSetup, "tick", lambda self: observed.set())
    app.state.settings = replace(app.state.settings, wifi_setup_enabled=True)
    with pytest.raises(ValueError):
        async with _lifespan(app):
            assert await asyncio.to_thread(observed.wait, 2)
            monitors = [thread for thread in threading.enumerate() if thread.name == "palmimo-wifi-setup"]
            raise ValueError("interrupted")
    assert monitors
    assert not any(thread.is_alive() for thread in monitors)


async def test_lifespan_finishes_other_cleanup_if_setup_shutdown_is_cancelled(
    app: FastAPI,
    adapters: FakeAdapterBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup: list[str] = []
    shutdown = app.state.wifi_setup.shutdown

    def cancel() -> None:
        shutdown()
        raise asyncio.CancelledError

    monkeypatch.setattr(app.state.wifi_setup, "shutdown", cancel)
    monkeypatch.setattr(adapters.wifi_attempts, "stop_recovery", lambda: cleanup.append("recovery"))
    with pytest.raises(asyncio.CancelledError):
        async with _lifespan(app):
            pass
    assert cleanup == ["recovery"]
