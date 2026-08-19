"""Server-side unprovisioned-state gating across the whole API.

The technical design is explicit that this is a server-side gate, not a
frontend routing convenience: only the Wi-Fi endpoints, auth setup, and
system status stay reachable while unprovisioned. Every other endpoint,
authenticated or not, answers 409 ``not_provisioned``.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def test_ssh_keys_list_is_blocked_while_unprovisioned(client: TestClient) -> None:
    response = client.get("/api/v1/ssh-keys")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_provisioned"


def test_system_reboot_is_blocked_while_unprovisioned(client: TestClient) -> None:
    response = client.post("/api/v1/system/reboot", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_provisioned"


def test_system_shutdown_is_blocked_while_unprovisioned(client: TestClient) -> None:
    response = client.post("/api/v1/system/shutdown", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_provisioned"


def test_login_is_blocked_while_unprovisioned(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"password": "x"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_provisioned"


def test_auth_setup_passes_while_unprovisioned(client: TestClient) -> None:
    response = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_wifi_status_passes_while_unprovisioned(client: TestClient) -> None:
    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 200


def test_wifi_networks_passes_while_unprovisioned(client: TestClient) -> None:
    response = client.get("/api/v1/wifi/networks")

    assert response.status_code == 200


def test_wifi_connect_passes_while_unprovisioned(client: TestClient) -> None:
    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_system_status_passes_while_unprovisioned(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.status_code == 200
    assert response.json()["state"] == "unprovisioned"


def test_ssh_keys_gate_lifts_once_provisioned(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.known_networks.add("home")

    response = client.get("/api/v1/ssh-keys")

    # Now reachable in principle -- still 401 because no session is set,
    # which is a separate gate (require_auth) than the one under test here.
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
