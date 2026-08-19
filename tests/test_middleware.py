"""Tests for the HostGuard and CSRF middleware."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


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
