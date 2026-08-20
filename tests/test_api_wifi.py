"""Tests for ``/api/v1/wifi``."""

from __future__ import annotations

import json
import logging
import time

import pytest
from starlette.testclient import TestClient

from palmimo_portal.core.auth import hash_password
from palmimo_portal.core.wifi_attempt import GRACE_PERIOD_SECONDS
from palmimo_portal.ports import (
    AdapterUnavailableError,
    ConnectionState,
    Identity,
    WifiAttempt,
    WifiNetwork,
    WifiStatus,
)
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def test_status_reflects_the_network_ports_current_state(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.42")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 200
    body = response.json()
    assert body == {"state": "connected", "ssid": "home", "ip_address": "198.51.100.42"}


def test_networks_returns_the_fakes_scan_results(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.scanned_networks = [
        WifiNetwork(ssid="home", signal=80, secured=True),
        WifiNetwork(ssid="guest", signal=40, secured=False),
    ]

    response = client.get("/api/v1/wifi/networks")

    assert response.status_code == 200
    assert response.json() == [
        {"ssid": "home", "signal": 80, "secured": True},
        {"ssid": "guest", "signal": 40, "secured": False},
    ]


def test_status_maps_adapter_unavailable_to_a_503_envelope(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.raise_on_get_status = AdapterUnavailableError("network_backend_unavailable", "comitup down")

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "network_backend_unavailable"


def test_networks_maps_adapter_unavailable_to_a_503_envelope(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.raise_on_list_networks = AdapterUnavailableError("network_backend_unavailable", "scan timed out")

    response = client.get("/api/v1/wifi/networks")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "network_backend_unavailable"


def test_connect_returns_attempting_immediately(client: TestClient) -> None:
    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "attempting"}


def test_connect_records_the_attempt_on_the_network_port(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert adapters.network.connect_calls == [("home", "secret123")]


def test_connect_records_the_attempt_in_the_state_store(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)

    attempt = adapters.state.read_last_wifi_attempt()
    assert attempt is not None
    assert attempt.ssid == "home"
    assert attempt.result == "attempting"


def test_system_status_exposes_the_last_wifi_attempt(client: TestClient) -> None:
    client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)

    response = client.get("/api/v1/system/status")

    assert response.json()["last_wifi_attempt"]["ssid"] == "home"
    assert response.json()["last_wifi_attempt"]["result"] == "attempting"


def test_connect_records_a_failed_attempt_when_network_connect_raises(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    adapters.network.raise_on_connect = RuntimeError("radio busy")

    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "wifi_connect_failed"
    attempt = adapters.state.read_last_wifi_attempt()
    assert attempt is not None
    assert attempt.ssid == "home"
    assert attempt.result == "failed"


def test_connect_records_attempting_before_calling_the_network_port(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    order: list[str] = []
    real_write = adapters.state.write_last_wifi_attempt
    real_connect = adapters.network.connect

    def recording_write(attempt: WifiAttempt) -> None:
        order.append("write")
        real_write(attempt)

    def recording_connect(ssid: str, psk: str) -> None:
        order.append("connect")
        real_connect(ssid, psk)

    adapters.state.write_last_wifi_attempt = recording_write  # type: ignore[method-assign]  # deliberate monkeypatch of a bound method for this test
    adapters.network.connect = recording_connect  # type: ignore[method-assign]  # deliberate monkeypatch of a bound method for this test

    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert order[0] == "write"
    assert "connect" in order


# -- connect: ssid/psk validation at the API boundary ------------------------
#
# A malformed ssid or psk must be rejected before any state write or adapter
# call -- see the module docstring's "verified" scenario: a lone-surrogate
# ssid (valid JSON to the stdlib decoder, not valid UTF-8) used to be
# persisted as the last attempt, and every later `GET /system/status` 500'd
# trying to serialize it back out. These tests exercise the boundary check
# directly, not the self-heal (that's test_state_adapter.py's).


def test_connect_rejects_a_lone_surrogate_ssid(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # httpx's own `json=` param round-trips through `ensure_ascii=False`,
    # which refuses to encode a lone surrogate client-side before the
    # request ever reaches this server -- the same UnicodeEncodeError this
    # fix guards against, just raised a layer too early to exercise the
    # server's validation. `ensure_ascii=True` (stdlib json's default)
    # escapes it to the ASCII-safe `"\ud800"`, matching how the real
    # attacker-controlled request body (crafted directly, not through
    # httpx) reaches the server: valid JSON the stdlib decoder happily
    # parses back into a `str` containing the lone surrogate.
    body = json.dumps({"ssid": "\ud800", "psk": "secret123"}, ensure_ascii=True).encode("ascii")
    response = client.post(
        "/api/v1/wifi/connect",
        content=body,
        headers={**CSRF_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wifi_invalid_ssid"
    assert adapters.state.read_last_wifi_attempt() is None
    assert adapters.network.connect_calls == []

    status_response = client.get("/api/v1/system/status")
    assert status_response.status_code == 200


def test_connect_rejects_a_nul_byte_in_ssid(client: TestClient, adapters: FakeAdapterBundle) -> None:
    response = client.post(
        "/api/v1/wifi/connect", json={"ssid": "home\x00net", "psk": "secret123"}, headers=CSRF_HEADERS
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wifi_invalid_ssid"
    assert adapters.state.read_last_wifi_attempt() is None


def test_connect_rejects_a_33_byte_ssid(client: TestClient) -> None:
    response = client.post("/api/v1/wifi/connect", json={"ssid": "a" * 33, "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wifi_invalid_ssid"


def test_connect_accepts_a_32_byte_multibyte_ssid(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # Ten copies of a 3-byte-in-UTF-8 character plus "ab" = 32 bytes exactly,
    # well under 32 *characters* -- the limit is bytes, not code points.
    ssid = "あ" * 10 + "ab"
    assert len(ssid.encode("utf-8")) == 32

    response = client.post("/api/v1/wifi/connect", json={"ssid": ssid, "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert adapters.network.connect_calls == [(ssid, "secret123")]


def test_connect_rejects_a_too_short_psk(client: TestClient, adapters: FakeAdapterBundle) -> None:
    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "1234567"}, headers=CSRF_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wifi_invalid_psk"
    assert adapters.state.read_last_wifi_attempt() is None
    assert adapters.network.connect_calls == []


def test_connect_accepts_a_64_hex_char_psk(client: TestClient, adapters: FakeAdapterBundle) -> None:
    psk = "ab" * 32
    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": psk}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert adapters.network.connect_calls == [("home", psk)]


def test_connect_rejects_a_65_char_psk(client: TestClient) -> None:
    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "a" * 65}, headers=CSRF_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wifi_invalid_psk"


def test_connect_accepts_an_empty_psk_for_an_open_network(client: TestClient, adapters: FakeAdapterBundle) -> None:
    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": ""}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert adapters.network.connect_calls == [("home", "")]


def test_connect_rejects_a_psk_with_a_non_printable_char(client: TestClient) -> None:
    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret\x01x"}, headers=CSRF_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wifi_invalid_psk"


# -- reconfigure-while-connected: operator visibility ------------------------


def test_connect_while_connected_logs_a_warning_naming_the_old_and_new_network(
    client: TestClient, adapters: FakeAdapterBundle, caplog: pytest.LogCaptureFixture
) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="OldNet", ip_address="198.51.100.7")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        client.post("/api/v1/wifi/connect", json={"ssid": "NewNet", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert "wifi reconfigure: forgetting 'OldNet' to connect to 'NewNet'" in caplog.text


def test_connect_while_not_connected_does_not_log_the_reconfigure_warning(
    client: TestClient, adapters: FakeAdapterBundle, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        client.post("/api/v1/wifi/connect", json={"ssid": "NewNet", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert "wifi reconfigure:" not in caplog.text


def test_connect_while_connected_records_a_failed_attempt_when_connect_raises_after_delete_connection(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # The reconfigure-while-connected path (see comitup.py's connect())
    # deletes the old profile and clears the known-network marker *before*
    # calling connect() again -- if that second call then raises, the
    # marker is already gone (adapter-level:
    # test_connect_while_connected_clears_known_marker_before_reconnecting
    # in test_comitup_adapter.py). This is the API-level half: the attempt
    # must still resolve to `failed`, not get stuck `attempting` or 200.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="OldNet", ip_address="198.51.100.7")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.raise_on_connect = AdapterUnavailableError("network_backend_unavailable", "boom")

    response = client.post("/api/v1/wifi/connect", json={"ssid": "NewNet", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "wifi_connect_failed"
    attempt = adapters.state.read_last_wifi_attempt()
    assert attempt is not None
    assert attempt.ssid == "NewNet"
    assert attempt.result == "failed"


# -- last_wifi_attempt lifecycle end-to-end: a later transition resolves it --


def test_last_wifi_attempt_resolves_to_connected_after_the_network_transitions(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)
    attempt = adapters.state.read_last_wifi_attempt()
    assert attempt is not None
    assert attempt.result == "attempting"

    # Simulates what the real adapter's next status poll would observe once
    # comitup actually finishes connecting -- see
    # FakeNetworkPort.simulate_transition's docstring.
    adapters.network.simulate_transition(ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.7")

    response = client.get("/api/v1/system/status")
    attempt = response.json()["last_wifi_attempt"]
    assert attempt["ssid"] == "home"
    assert attempt["result"] == "connected"


def test_last_wifi_attempt_resolves_to_failed_after_falling_back_to_the_hotspot(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "wrong-password"}, headers=CSRF_HEADERS)
    attempt = adapters.state.read_last_wifi_attempt()
    assert attempt is not None
    assert attempt.result == "attempting"

    # Back-date the attempt past the grace period (see
    # palmimo_portal.core.wifi_attempt.GRACE_PERIOD_SECONDS): a HOTSPOT
    # observation only resolves an attempt old enough that comitup could
    # plausibly have tried and failed already -- a just-written attempt is
    # deliberately protected from an immediately-following HOTSPOT
    # observation, since comitup may not have even started trying yet.
    adapters.state.write_last_wifi_attempt(
        WifiAttempt(ssid=attempt.ssid, result=attempt.result, timestamp=time.time() - GRACE_PERIOD_SECONDS - 1)
    )

    # comitup gave up and fell back to its own AP -- the only failure
    # signal this adapter ever gets (comitup provides no failure reason).
    adapters.network.simulate_transition(ConnectionState.UNPROVISIONED)

    response = client.get("/api/v1/system/status")
    attempt = response.json()["last_wifi_attempt"]
    assert attempt["ssid"] == "home"
    assert attempt["result"] == "failed"


def test_wifi_endpoints_require_a_session_once_provisioned(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.known_networks.add("home")

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_wifi_endpoints_are_open_again_with_a_valid_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 200


# -- F4: a DIY device that already set a password must not reopen wifi ------
# -- just because it is (again) unprovisioned --------------------------------


def test_wifi_requires_a_session_when_password_already_set_even_while_unprovisioned(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A DIY device that already completed setup (auth_state == "set") but
    # is currently unprovisioned (e.g. it forgot its Wi-Fi network) must
    # not fall back to the bootstrap-only "open while unprovisioned"
    # behavior -- that behavior exists only for a device that has never
    # had a password set at all.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    assert adapters.network.known_networks == set()  # still unprovisioned

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_login_still_works_while_unprovisioned_once_a_password_is_set(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Without this, a DIY device that set a password and later became
    # unprovisioned again could never log back in: require_provisioned_unless_identity
    # would otherwise block /auth/login until Wi-Fi is configured, but Wi-Fi
    # itself is now session-gated (the test above) -- a deadlock.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_wifi_reachable_after_logging_in_while_unprovisioned_with_a_password_already_set(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 200


# -- DELETE /api/v1/wifi/connection (forget the current network) ------------


def _setup_and_login(client: TestClient) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)


def test_forget_returns_200_and_the_fake_records_the_call(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.42")
    adapters.network.known_networks.add("home")
    _setup_and_login(client)

    response = client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "forgetting"}
    assert adapters.network.forget_calls == ["home"]


def test_forget_does_not_write_a_last_wifi_attempt(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.42")
    adapters.network.known_networks.add("home")
    _setup_and_login(client)

    client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)

    assert adapters.state.read_last_wifi_attempt() is None


def test_forget_requires_authentication(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.42")
    adapters.network.known_networks.add("home")

    response = client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_forget_rejects_an_initial_mode_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.identity.identity = Identity(device_id="0001", initial_password_hash=hash_password("sticker-pw"))
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.42")
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "sticker-pw"}, headers=CSRF_HEADERS)

    response = client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "initial_password_must_be_changed"


def test_forget_requires_provisioning(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _setup_and_login(client)  # still unprovisioned: no known networks

    response = client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_provisioned"


def test_forget_maps_adapter_unavailable_to_a_503_envelope(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.42")
    adapters.network.known_networks.add("home")
    _setup_and_login(client)
    adapters.network.raise_on_forget = AdapterUnavailableError("network_backend_unavailable", "comitup down")

    response = client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "network_backend_unavailable"


def test_forget_returns_409_when_not_actually_connected(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # Provisioned (has a known network on file) but not currently CONNECTED
    # -- e.g. it fell back to its own hotspot after losing the home
    # network. The fake mirrors ComitupNetworkPort.forget_current's
    # fresh-state check: forgetting must not silently no-op or delete the
    # wrong profile, it must be refused with a 409 the frontend can show.
    adapters.network.status = WifiStatus(state=ConnectionState.UNPROVISIONED, ssid=None, ip_address=None)
    adapters.network.known_networks.add("home")
    _setup_and_login(client)

    response = client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "wifi_not_connected"
    assert adapters.network.forget_calls == []


def test_forget_leaves_the_device_reporting_unprovisioned_when_no_other_known_network(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Forgetting the only known network puts the device back in the
    # out-of-box state: GET /system/status must say so (core.provisioning.
    # is_provisioned is False) rather than continuing to claim "known" from
    # a network that no longer exists.
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.42")
    adapters.network.known_networks.add("home")
    _setup_and_login(client)

    delete_response = client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)
    assert delete_response.status_code == 200

    status_response = client.get("/api/v1/system/status")
    assert status_response.json()["state"] == "unprovisioned"


def test_forget_maps_a_generic_failure_to_a_502_envelope(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.42")
    adapters.network.known_networks.add("home")
    _setup_and_login(client)
    adapters.network.raise_on_forget = RuntimeError("boom")

    response = client.delete("/api/v1/wifi/connection", headers=CSRF_HEADERS)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "wifi_forget_failed"
