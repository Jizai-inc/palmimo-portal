"""Tests for ``/api/v1/wifi/saved``."""

from __future__ import annotations

from starlette.testclient import TestClient

from palmimo_portal.ports import AdapterUnavailableError, ConnectionState, WifiStatus
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def _provisioned_and_logged_in(client: TestClient, adapters: FakeAdapterBundle, *saved: str) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid=saved[0], ip_address="198.51.100.7")
    adapters.network.known_networks.add(saved[0])
    adapters.saved_networks.saved = list(saved)
    adapters.saved_networks.active = saved[0]
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)


def test_list_saved_returns_the_networks_in_order_marking_the_active_one(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _provisioned_and_logged_in(client, adapters, "home", "phone")

    response = client.get("/api/v1/wifi/saved")

    assert response.status_code == 200
    assert response.json() == [{"ssid": "home", "active": True}, {"ssid": "phone", "active": False}]


def test_list_saved_requires_authentication(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.7")
    adapters.network.known_networks.add("home")

    response = client.get("/api/v1/wifi/saved")

    assert response.status_code == 401


def test_save_adds_a_network_last_without_switching(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provisioned_and_logged_in(client, adapters, "home")

    response = client.post("/api/v1/wifi/saved", json={"ssid": "cafe", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert [item["ssid"] for item in response.json()] == ["home", "cafe"]
    assert adapters.saved_networks.psks["cafe"] == "secret123"
    assert adapters.saved_networks.activate_calls == []
    assert adapters.network.connect_calls == []


def test_save_rejects_a_too_short_psk(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provisioned_and_logged_in(client, adapters, "home")

    response = client.post("/api/v1/wifi/saved", json={"ssid": "cafe", "psk": "short"}, headers=CSRF_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wifi_invalid_psk"
    assert adapters.saved_networks.saved == ["home"]


def test_delete_saved_removes_a_network_other_than_the_current_one(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _provisioned_and_logged_in(client, adapters, "home", "cafe")

    response = client.delete("/api/v1/wifi/saved/cafe", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert [item["ssid"] for item in response.json()] == ["home"]


def test_delete_saved_refuses_the_current_network_with_409(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provisioned_and_logged_in(client, adapters, "home", "cafe")

    response = client.delete("/api/v1/wifi/saved/home", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "wifi_saved_active"
    assert adapters.saved_networks.saved == ["home", "cafe"]


def test_delete_saved_returns_404_for_an_unknown_network(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provisioned_and_logged_in(client, adapters, "home")

    response = client.delete("/api/v1/wifi/saved/nowhere", headers=CSRF_HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "wifi_saved_not_found"


def test_reorder_saved_applies_the_requested_order(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provisioned_and_logged_in(client, adapters, "home", "cafe", "phone")

    response = client.put("/api/v1/wifi/saved/order", json={"ssids": ["phone", "home", "cafe"]}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert [item["ssid"] for item in response.json()] == ["phone", "home", "cafe"]


def test_reorder_saved_returns_409_when_the_set_differs(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provisioned_and_logged_in(client, adapters, "home", "cafe")

    response = client.put("/api/v1/wifi/saved/order", json={"ssids": ["home"]}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "wifi_saved_order_mismatch"
    assert adapters.saved_networks.saved == ["home", "cafe"]


def test_activate_saved_switches_and_records_the_attempt(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provisioned_and_logged_in(client, adapters, "home", "cafe")

    response = client.post("/api/v1/wifi/saved/cafe/activate", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "switching"}
    assert adapters.saved_networks.activate_calls == ["cafe"]
    attempt = adapters.state.read_last_wifi_attempt()
    assert attempt is not None
    assert (attempt.ssid, attempt.result) == ("cafe", "attempting")


def test_activate_saved_returns_404_and_records_a_failure_for_an_unknown_network(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _provisioned_and_logged_in(client, adapters, "home")

    response = client.post("/api/v1/wifi/saved/nowhere/activate", headers=CSRF_HEADERS)

    assert response.status_code == 404
    attempt = adapters.state.read_last_wifi_attempt()
    assert attempt is not None
    assert attempt.result == "failed"


def test_list_saved_maps_adapter_unavailable_to_a_503(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provisioned_and_logged_in(client, adapters, "home")
    adapters.saved_networks.raise_on_call = AdapterUnavailableError("network_backend_unavailable", "boom")

    response = client.get("/api/v1/wifi/saved")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "network_backend_unavailable"
