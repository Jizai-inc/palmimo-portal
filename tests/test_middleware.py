"""Tests for the HostGuard and CSRF middleware."""

from __future__ import annotations

import asyncio
import socket
import time as time_module

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api import app as app_module
from palmimo_portal.api.app import CAPTIVE_PROBE_HOSTS, HostGuardMiddleware
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


@pytest.mark.parametrize("probe_host", sorted(CAPTIVE_PROBE_HOSTS))
def test_unprovisioned_captive_probe_redirects_to_this_machine(client: TestClient, probe_host: str) -> None:
    response = client.get("/hotspot-detect.html", headers={"Host": probe_host}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == CAPTIVE_PROBE_LOCATION


def test_unprovisioned_captive_probe_head_redirects(client: TestClient) -> None:
    response = client.head("/hotspot-detect.html", headers={"Host": "captive.apple.com"}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == CAPTIVE_PROBE_LOCATION


def test_provisioned_captive_probe_host_is_rejected_with_421(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.known_networks.add("home")

    response = client.get("/hotspot-detect.html", headers={"Host": "captive.apple.com"})

    assert response.status_code == 421


def test_unprovisioned_captive_probe_host_on_api_path_is_rejected_with_421(client: TestClient) -> None:
    response = client.get("/api/v1/system/status", headers={"Host": "captive.apple.com"})

    assert response.status_code == 421


def test_unprovisioned_captive_probe_host_on_bare_api_path_is_rejected_with_421(client: TestClient) -> None:
    response = client.get("/api", headers={"Host": "captive.apple.com"})

    assert response.status_code == 421


def test_unprovisioned_captive_probe_host_post_is_rejected_with_421(client: TestClient) -> None:
    response = client.post("/hotspot-detect.html", headers={"Host": "captive.apple.com", **CSRF_HEADERS})

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

    assert asyncio.run(middleware._is_provisioned()) is False

    network.status = WifiStatus(state=ConnectionState.CONNECTED, ssid="home", ip_address="198.51.100.7")

    # Within the TTL window, the cached (stale) unprovisioned result is reused.
    assert asyncio.run(middleware._is_provisioned()) is False

    clock["t"] += app_module._PROVISIONED_CACHE_TTL_SECONDS + 1.0
    assert asyncio.run(middleware._is_provisioned()) is True


def test_dispatch_uses_is_provisioned_to_decide_the_redirect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake network port defaults to unprovisioned, which would 302 on
    # its own -- forcing _is_provisioned to report True and asserting the
    # 421 instead proves dispatch actually awaits and branches on it,
    # rather than deciding the redirect some other way.
    calls = {"n": 0}

    async def _forced_provisioned(self: HostGuardMiddleware) -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(HostGuardMiddleware, "_is_provisioned", _forced_provisioned)

    response = client.get("/hotspot-detect.html", headers={"Host": "captive.apple.com"})

    assert calls["n"] == 1
    assert response.status_code == 421


def test_concurrent_probe_burst_calls_the_network_port_once() -> None:
    calls = {"n": 0}

    class SlowNetworkPort(FakeNetworkPort):
        def get_status(self) -> WifiStatus:
            calls["n"] += 1
            time_module.sleep(0.05)
            return super().get_status()

    network = SlowNetworkPort()
    app = FastAPI()

    @app.get("/hotspot-detect.html")
    async def _probe() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(HostGuardMiddleware, always_allowed_hosts=frozenset(), network=network)

    async def _fire_burst() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            responses = await asyncio.gather(
                *[ac.get("/hotspot-detect.html", headers={"Host": "captive.apple.com"}) for _ in range(5)]
            )
        return [r.status_code for r in responses]

    statuses = asyncio.run(_fire_burst())

    assert statuses == [302] * 5
    assert calls["n"] == 1
