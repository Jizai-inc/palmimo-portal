from __future__ import annotations

import dataclasses
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api.app import create_app
from palmimo_portal.core.wifi_attempt_manager import WifiAttemptManager, connect_legacy
from palmimo_portal.ports import (
    AdapterUnavailableError,
    ConnectionState,
    PolkitDeniedError,
    WifiDeviceStatus,
    WifiStatus,
)
from palmimo_portal.settings import Settings
from tests.test_wifi_attempt_manager import Rig


HEADERS = {"X-Requested-With": "PalmimoPortal"}


def install(app: FastAPI, rig: Rig) -> Any:
    bundle = app.state.adapters
    network, saved = bundle.network, bundle.saved_networks
    network.profiles = saved.profiles = rig.nm
    manager = WifiAttemptManager(
        rig.nm,
        rig.service,
        rig.state,
        edit_lock=rig.lock,
        monotonic=lambda: rig.now,
        sleep=rig.sleep,
        spawn=rig.pending.append,
        spawn_recovery=lambda work: work(),
        legacy_connect=partial(connect_legacy, network, saved),
        legacy_status=network.get_status,
    )
    network.known_profiles = manager.has_known_networks
    network.display_ssid = manager.display_ssid
    app.state.adapters = dataclasses.replace(
        bundle, state=rig.state, wifi_profiles=rig.nm, comitup_service=rig.service, wifi_attempts=manager
    )
    return app.state.adapters


def login(client: TestClient) -> None:
    assert client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=HEADERS).status_code == 200
    assert client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=HEADERS).status_code == 200


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/connect", {"ssid": "other", "psk": "wrongpass"}),
        ("POST", "/saved/activate", {"ssid": "home"}),
        ("POST", "/saved", {"ssid": "other", "psk": "password"}),
        ("POST", "/saved/delete", {"ssid": "home"}),
        ("PUT", "/saved/order", {"ssids": ["home"]}),
        ("DELETE", "/connection", None),
    ],
)
def test_pending_attempt_blocks_other_wifi_writes(app: FastAPI, method: str, path: str, body: Any) -> None:
    rig = Rig()
    rig.add()
    install(app, rig)
    with TestClient(app) as client:
        login(client)
        assert (
            client.post("/api/v1/wifi/connect", json={"ssid": "away", "psk": "wrongpass"}, headers=HEADERS).status_code
            == 200
        )
        before = rig.snapshot()
        response = client.request(method, "/api/v1/wifi" + path, json=body, headers=HEADERS)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "wifi_attempt_in_progress"
        assert rig.snapshot() == before


@pytest.mark.parametrize(
    "error,code",
    [
        (AdapterUnavailableError("system_backend_unavailable", "offline"), "wifi_kick_unavailable"),
    ],
)
def test_startup_service_failure_returns_503_when_connecting(app: FastAPI, error: Exception, code: str) -> None:
    rig = Rig()
    rig.service.raise_on_call = error
    install(app, rig)
    with TestClient(app) as client:
        response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "password"}, headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == code
        assert rig.nm.list_profiles() == []


@pytest.mark.parametrize("legacy", ["polkit", "replacement"])
def test_legacy_connect_uses_comitup_without_temporary_profiles(app: FastAPI, legacy: str) -> None:
    rig = Rig()
    if legacy == "polkit":
        rig.service.raise_on_call = PolkitDeniedError("comitup.service", "start")
    bundle = install(app, rig)
    ssid = "home" if legacy == "polkit" else "\ufffdhome"
    with TestClient(app) as client:
        response = client.post("/api/v1/wifi/connect", json={"ssid": ssid, "psk": "password"}, headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == {"status": "attempting"}
        assert bundle.network.connect_calls == [(ssid, "password")]
        assert all(not p.name.startswith("palmimo-try-") for p in rig.nm.list_profiles())
        assert rig.state.read_last_wifi_attempt().result == "attempting"  # type: ignore[union-attr]


@pytest.mark.parametrize("active", [False, True])
def test_startup_recovers_marks_and_updates_last_attempt_before_serving(app: FastAPI, active: bool) -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    rig.manager().start_wifi_attempt("credentials", b"home", "wrongpass", None)
    if active:
        target = next(p.uuid for p in rig.nm.list_profiles() if p.name.startswith("palmimo-try-"))
        rig.nm.device = WifiDeviceStatus(100, target)
    install(app, rig)
    with TestClient(app) as client:
        assert client.get("/api/v1/system/status").json()["last_wifi_attempt"]["result"] == (
            "connected" if active else "failed"
        )
        if not active:
            assert rig.snapshot() == before
        else:
            assert [(p.uuid, p.name) for p in rig.nm.list_profiles()] == [(target, "home")]


@pytest.mark.parametrize("path", ["/status", "/networks", "/connect"])
@pytest.mark.parametrize("connected", [False, True])
def test_nm_failure_keeps_diy_wifi_access_closed(app: FastAPI, path: str, connected: bool) -> None:
    rig = Rig()
    bundle = install(app, rig)
    if connected:
        bundle.network.status = WifiStatus(ConnectionState.CONNECTED, "home", None)
    with TestClient(app) as client:
        rig.nm.raise_on_call = AdapterUnavailableError("network_backend_unavailable", "offline")
        response = (
            client.post("/api/v1/wifi" + path, json={"ssid": "home", "psk": "password"}, headers=HEADERS)
            if path == "/connect"
            else client.get("/api/v1/wifi" + path)
        )
        assert response.status_code == (401 if connected else 503)
        assert response.json()["error"]["code"] == ("not_authenticated" if connected else "network_backend_unavailable")


def test_internal_profiles_are_hidden_and_parked_status_uses_original_ssid(app: FastAPI) -> None:
    rig = Rig()
    rig.add()
    bundle = install(app, rig)
    with TestClient(app) as client:
        login(client)
        client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "wrongpass"}, headers=HEADERS)
        parked = next(p for p in rig.nm.list_profiles() if p.user_data.get("palmimo.attempt.role") == "parked")
        bundle.network.status = WifiStatus(ConnectionState.CONNECTING, parked.ssid.decode(), None)
        assert client.get("/api/v1/wifi/saved").json() == []
        assert client.get("/api/v1/wifi/status").json()["ssid"] == "home"
        assert rig.state.read_last_wifi_attempt().result == "attempting"  # type: ignore[union-attr]


@pytest.mark.parametrize("same_ssid", [False, True])
def test_api_failed_attempt_restores_original_profiles_and_passwords(app: FastAPI, same_ssid: bool) -> None:
    rig = Rig()
    home = rig.add()
    rig.nm.device = WifiDeviceStatus(100, "setup-ap" if same_ssid else home)
    before = rig.snapshot()
    bundle = install(app, rig)
    bundle.network.status = WifiStatus(
        ConnectionState.UNPROVISIONED if same_ssid else ConnectionState.CONNECTED, None if same_ssid else "home", None
    )
    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/v1/wifi/connect", json={"ssid": "home" if same_ssid else "away", "psk": "wrongpass"}, headers=HEADERS
        )
        assert response.json() == {"status": "attempting"}
        rig.tick = lambda: (
            setattr(rig.nm, "device", WifiDeviceStatus(100, "setup-ap" if same_ssid else home))
            if rig.now >= (35 if same_ssid else 16)
            else None
        )
        rig.run()
        assert rig.snapshot() == before
        assert client.get("/api/v1/system/status").json()["last_wifi_attempt"]["result"] == "failed"


@pytest.mark.parametrize("legacy", ["polkit", "replacement"])
@pytest.mark.parametrize("activate", [False, True])
def test_legacy_connected_switch_uses_saved_profiles(app: FastAPI, legacy: str, activate: bool) -> None:
    rig = Rig()
    home = rig.add()
    target = "other" if legacy == "polkit" else "\ufffdother"
    if activate:
        rig.add(target.encode())
    if legacy == "polkit":
        rig.service.raise_on_call = PolkitDeniedError("comitup.service", "start")
    bundle = install(app, rig)
    bundle.network.status = WifiStatus(ConnectionState.CONNECTED, "home", None)
    rig.nm.device = WifiDeviceStatus(100, home)
    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/v1/wifi/saved/activate" if activate else "/api/v1/wifi/connect",
            json={"ssid": target, "psk": "password"},
            headers=HEADERS,
        )
        assert response.json() == {"status": "attempting"}
        assert bundle.saved_networks.activate_calls == [target]
        assert bundle.network.connect_calls == []
        assert rig.nm.psks[home] == "correct"
        assert all(not p.name.startswith("palmimo-try-") for p in rig.nm.list_profiles())


@pytest.mark.parametrize("connected", [False, True])
def test_legacy_worker_owns_result_and_status_polls_cannot_change_it(app: FastAPI, connected: bool) -> None:
    rig = Rig()
    bundle = install(app, rig)
    with TestClient(app) as client:
        login(client)
        client.post("/api/v1/wifi/connect", json={"ssid": "\ufffdhome", "psk": "password"}, headers=HEADERS)
        bundle.network.status = WifiStatus(ConnectionState.CONNECTED, "\ufffdhome", None)
        assert client.get("/api/v1/system/status").json()["last_wifi_attempt"]["result"] == "attempting"
        rig.tick = lambda: setattr(
            bundle.network,
            "status",
            WifiStatus(
                ConnectionState.CONNECTED if connected else ConnectionState.UNPROVISIONED,
                "\ufffdhome" if connected else None,
                None,
            ),
        )
        rig.run()
        expected = "connected" if connected else "failed"
        assert client.get("/api/v1/system/status").json()["last_wifi_attempt"]["result"] == expected
        bundle.network.status = WifiStatus(
            ConnectionState.UNPROVISIONED if connected else ConnectionState.CONNECTED, "other", None
        )
        client.get("/api/v1/wifi/status")
        assert client.get("/api/v1/system/status").json()["last_wifi_attempt"]["result"] == expected


@pytest.mark.parametrize("backend", [False, True])
def test_cleanup_error_is_reported_when_start_cannot_recover_leftover_marks(app: FastAPI, backend: bool) -> None:
    rig = Rig()
    rig.add()
    install(app, rig)
    with TestClient(app) as client:
        login(client)
        client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "wrongpass"}, headers=HEADERS)
        rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, "setup-ap"))

        def fail_delete(operation: str) -> None:
            if operation == "delete":
                raise (
                    AdapterUnavailableError("network_backend_unavailable", "offline") if backend else OSError("offline")
                )

        rig.nm.fault = fail_delete
        rig.run()
        response = client.post("/api/v1/wifi/connect", json={"ssid": "other", "psk": "password"}, headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == ("network_backend_unavailable" if backend else "wifi_cleanup_failed")


def test_old_marker_file_cannot_make_a_diy_device_provisioned(settings: Settings, tmp_path: Path) -> None:
    marker = tmp_path / "network_known.marker"
    marker.touch()
    app = create_app(dataclasses.replace(settings, state_dir=tmp_path))
    with TestClient(app) as client:
        response = client.get("/api/v1/wifi/status")
        assert response.status_code == 200
        assert response.json()["state"] == "unprovisioned"
        assert marker.exists()


def test_connected_apps_remain_available_when_nm_profile_reads_fail(app: FastAPI) -> None:
    rig = Rig()
    rig.add()
    bundle = install(app, rig)
    bundle.network.status = WifiStatus(ConnectionState.CONNECTED, "home", None)
    with TestClient(app) as client:
        login(client)
        rig.nm.raise_on_call = AdapterUnavailableError("network_backend_unavailable", "offline")
        assert client.get("/api/v1/apps").status_code == 200


def test_http_serves_during_background_recovery_and_wifi_writes_are_busy(app: FastAPI) -> None:
    rig = Rig()
    rig.add()
    rig.manager().start_wifi_attempt("credentials", b"home", "password", None)
    rig.pending.clear()
    bundle = install(app, rig)
    bundle.wifi_attempts._spawn_recovery = rig.pending.append
    target = next(p.uuid for p in rig.nm.list_profiles() if p.name.startswith("palmimo-try-"))
    with TestClient(app) as client:
        login(client)
        assert client.get("/api/v1/system/status").status_code == 200
        for path, body in [
            ("/connect", {"ssid": "other", "psk": "password"}),
            ("/saved", {"ssid": "other", "psk": "password"}),
        ]:
            response = client.post("/api/v1/wifi" + path, json=body, headers=HEADERS)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "wifi_attempt_in_progress"
        rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target)) if rig.now >= 5 else None
        rig.run()
        assert client.get("/api/v1/system/status").json()["last_wifi_attempt"]["result"] == "connected"


def test_a_real_ssid_with_trial_prefix_remains_visible(app: FastAPI) -> None:
    rig = Rig()
    bundle = install(app, rig)
    bundle.network.status = WifiStatus(ConnectionState.CONNECTED, "palmimo-try-home", None)
    with TestClient(app) as client:
        login(client)
        assert client.get("/api/v1/wifi/status").json()["ssid"] == "palmimo-try-home"
