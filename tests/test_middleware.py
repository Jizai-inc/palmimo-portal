"""Tests for the HostGuard and CSRF middleware."""

from __future__ import annotations

import socket

from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api import app as app_module
from palmimo_portal.api.app import HostGuardMiddleware
from palmimo_portal.ports import ConnectionState, WifiStatus
from palmimo_portal.testing.fakes import FakeAdapterBundle, FakeNetworkPort


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}
CAPTIVE_PROBE_LOCATION = f"http://{socket.gethostname()}.local/"


def test_evil_host_header_is_rejected_with_421(client: TestClient) -> None:
    response = client.get("/api/v1/system/status", headers={"Host": "evil.example.com"})

    assert response.status_code == 421
    assert response.json()["error"]["code"] == "host_not_allowed"


def test_allowed_host_passes_hostguard(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.status_code == 200


def test_extra_allowed_host_from_settings_passes(app: FastAPI) -> None:
    client = TestClient(app, headers={"Host": "testserver"})
    response = client.get("/api/v1/system/status")

    assert response.status_code == 200


def test_post_without_csrf_header_is_rejected_with_403(client: TestClient) -> None:
    response = client.post("/api/v1/auth/setup", json={"password": "hunter2"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_header_missing"


def test_post_with_csrf_header_passes_csrf(client: TestClient) -> None:
    response = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_get_never_needs_the_csrf_header(client: TestClient) -> None:
    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 200


def test_hostguard_runs_before_csrf(client: TestClient) -> None:
    # A bad Host header on a state-changing request with no CSRF header
    # either would trip must resolve as the outer HostGuard's 421, not CSRF's 403.
    response = client.post("/api/v1/auth/setup", json={"password": "x"}, headers={"Host": "evil.example.com"})

    assert response.status_code == 421


def test_unprovisioned_apple_captive_probe_redirects_to_this_machine(client: TestClient) -> None:
    response = client.get(
        "/hotspot-detect.html", headers={"Host": "captive.apple.com"}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == CAPTIVE_PROBE_LOCATION


def test_unprovisioned_android_captive_probe_redirects(client: TestClient) -> None:
    response = client.get(
        "/generate_204", headers={"Host": "connectivitycheck.gstatic.com"}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == CAPTIVE_PROBE_LOCATION


def test_provisioned_captive_probe_host_is_rejected_with_421(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.known_networks.add("home")

    response = client.get("/hotspot-detect.html", headers={"Host": "captive.apple.com"})

    assert response.status_code == 421


def test_unprovisioned_captive_probe_host_on_api_path_is_rejected_with_421(client: TestClient) -> None:
    response = client.get("/api/v1/system/status", headers={"Host": "captive.apple.com"})

    assert response.status_code == 421


def test_unprovisioned_captive_probe_host_post_is_rejected_with_421(client: TestClient) -> None:
    response = client.post(
        "/hotspot-detect.html", headers={"Host": "captive.apple.com", **CSRF_HEADERS}
    )

    assert response.status_code == 421


def test_unprovisioned_non_probe_foreign_host_is_still_rejected_with_421(client: TestClient) -> None:
    response = client.get("/hotspot-detect.html", headers={"Host": "evil.example.com"})

    assert response.status_code == 421


def test_network_port_raising_fails_closed_to_421(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.raise_on_get_status = RuntimeError("dbus down")

    response = client.get("/hotspot-detect.html", headers={"Host": "captive.apple.com"})

    assert response.status_code == 421


def test_captive_probe_redirect_stops_once_ttl_observes_provisioning() -> None:
    network = FakeNetworkPort()
    clock = {"t": 0.0}
    middleware = HostGuardMiddleware(
        app=FastAPI(),
        always_allowed_hosts=frozenset(),
        network=network,
        clock=lambda: clock["t"],
    )

    assert middleware._is_provisioned() is False

    network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.7")

    # Within the TTL window, the cached (stale) unprovisioned result is reused.
    assert middleware._is_provisioned() is False

    clock["t"] += app_module._PROVISIONED_CACHE_TTL_SECONDS + 1.0
    assert middleware._is_provisioned() is True
