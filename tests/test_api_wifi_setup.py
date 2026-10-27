import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.ports import PreemptedApp
from palmimo_portal.testing.fakes import FakeAdapterBundle
from tests.test_wifi_setup_lifespan import install_app
from tests.test_wifi_setup_lifespan import settings as settings


def test_system_status_reports_setup_mode_and_screen(
    client: TestClient,
    app: FastAPI,
    adapters: FakeAdapterBundle,
) -> None:
    app.state.wifi_setup.tick()
    for _ in range(3):
        adapters.clock.monotonic_value += 2
        app.state.wifi_setup.tick()
    reader = adapters.wifi_qr_client.clients[-1]
    reader.emit({"type": "hello", "proto": 1, "display": "frame"})
    assert client.get("/api/v1/system/status").json()["wifi_setup"] == {
        "mode": "active",
        "screen": "scanning",
        "reason": None,
    }
    app.state.wifi_setup.stop("app")
    assert client.get("/api/v1/system/status").json()["wifi_setup"] == {"mode": "idle", "screen": None, "reason": None}


@pytest.mark.parametrize("devices,stops_reader", [('"camera"', True), ('"audio"', False)])
def test_web_app_start_arbitrates_with_running_reader(
    app: FastAPI,
    client: TestClient,
    adapters: FakeAdapterBundle,
    devices: str,
    stops_reader: bool,
) -> None:
    install_app(app, "face", autostart=False, devices=devices)
    headers = {"X-Requested-With": "PalmimoPortal"}
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=headers)
    adapters.saved_networks.save("home", "", first=False)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=headers)
    app.state.wifi_setup.tick()
    for _ in range(30):
        adapters.clock.monotonic_value += 2
        app.state.wifi_setup.tick()
    reader = adapters.wifi_qr_client.clients[-1]
    reader.emit({"type": "hello", "proto": 1, "display": "frame"})
    response = client.post("/api/v1/apps/face/start", headers=headers)
    assert response.status_code == 202
    assert adapters.app_unit.start_calls == ["face"]
    assert reader.closed is stops_reader
    assert app.state.wifi_setup.status().mode == ("idle" if stops_reader else "active")


def test_reader_disconnect_enters_backoff_through_wired_callback(
    app: FastAPI,
    client: TestClient,
    adapters: FakeAdapterBundle,
) -> None:
    app.state.wifi_setup.tick()
    for _ in range(3):
        adapters.clock.monotonic_value += 2
        app.state.wifi_setup.tick()
    reader = adapters.wifi_qr_client.clients[-1]
    reader.emit({"type": "hello", "proto": 1, "display": "frame"})
    reader.close()
    app.state.wifi_setup.tick()
    assert app.state.wifi_setup.status().mode == "backoff"
    assert adapters.wifi_qr_unit.status().active_state == "inactive"


def test_web_start_explains_pending_display_recovery(
    app: FastAPI,
    client: TestClient,
    adapters: FakeAdapterBundle,
) -> None:
    install_app(app, "face", autostart=False)
    headers = {"X-Requested-With": "PalmimoPortal"}
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=headers)
    adapters.saved_networks.save("home", "", first=False)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=headers)
    record = PreemptedApp("face", "display-firmware")
    adapters.state.write_preempted_app(record)
    response = client.post("/api/v1/apps/face/start", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "display_recovery_pending"
    assert adapters.state.read_preempted_app() == record
    assert adapters.app_unit.start_calls == []


def test_old_firmware_reason_is_reported_by_system_and_platform(
    app: FastAPI,
    client: TestClient,
    adapters: FakeAdapterBundle,
) -> None:
    from palmimo_portal.ports import WifiQrUnitStatus

    headers = {"X-Requested-With": "PalmimoPortal"}
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=headers)
    adapters.saved_networks.save("home", "", first=False)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=headers)
    app.state.wifi_setup.tick()
    for _ in range(30):
        adapters.clock.monotonic_value += 2
        app.state.wifi_setup.tick()
    adapters.wifi_qr_unit.unit_status = WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 76)
    app.state.wifi_setup.tick()
    system = client.get("/api/v1/system/status")
    platform = client.get("/api/v1/platform")
    assert system.status_code == platform.status_code == 200
    for response in (system, platform):
        assert response.json()["wifi_setup"] == {
            "mode": "unavailable",
            "screen": None,
            "reason": "firmware_too_old",
        }
    assert platform.json()["ready"] is True
