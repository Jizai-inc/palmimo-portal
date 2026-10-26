import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.ports import PreemptedApp, UnitStatus
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
    assert client.get("/api/v1/system/status").json()["wifi_setup"] == {"mode": "active", "screen": "scanning"}
    app.state.wifi_setup.stop("app")
    assert client.get("/api/v1/system/status").json()["wifi_setup"] == {"mode": "idle", "screen": None}


@pytest.mark.parametrize(
    "owner,active,marked",
    [
        ("wifi-setup", "inactive", True),
        ("wifi-setup", "active", False),
        ("wifi-setup", "failed", False),
        ("display-firmware", "inactive", False),
    ],
)
def test_app_summary_marks_only_stopped_wifi_preempted_app(
    app: FastAPI,
    client: TestClient,
    adapters: FakeAdapterBundle,
    owner: str,
    active: str,
    marked: bool,
) -> None:
    install_app(app, "face", autostart=False)
    install_app(app, "other", autostart=False, devices='"audio"')
    assert (
        client.post(
            "/api/v1/auth/setup", json={"password": "hunter2"}, headers={"X-Requested-With": "PalmimoPortal"}
        ).status_code
        == 200
    )
    adapters.saved_networks.save("home", "", first=False)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers={"X-Requested-With": "PalmimoPortal"})
    adapters.state.write_preempted_app(PreemptedApp("face", owner))
    adapters.app_unit.simulate_active_state(
        "face", UnitStatus(active, "", "exit-code" if active == "failed" else "success", 1)
    )
    response = client.get("/api/v1/apps")
    assert response.status_code == 200
    apps = {item["id"]: item for item in response.json()["apps"]}
    assert apps["face"]["wifi_setup_preempted"] is marked
    assert apps["other"]["wifi_setup_preempted"] is False


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
