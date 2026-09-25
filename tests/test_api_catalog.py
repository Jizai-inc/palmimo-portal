"""Tests for ``GET /api/v1/catalog`` (design doc 3.2/4.1)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.ports import CatalogAsset, CatalogEnvSpec
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle, make_catalog_app


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(allowed_hosts=frozenset({"testserver"}), static_dir=tmp_path / "static-not-built")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    from palmimo_portal.api.app import create_app

    return create_app(settings)


@pytest.fixture
def adapters(app: FastAPI) -> FakeAdapterBundle:
    return app.state.adapters


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _authenticated_client(client: TestClient, adapters: FakeAdapterBundle) -> TestClient:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    return client


def test_get_catalog_returns_the_fetched_apps(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.catalog.asset = CatalogAsset(tag="v1.0.0", apps=(make_catalog_app("palmimo-teleop"),))
    _authenticated_client(client, adapters)

    response = client.get("/api/v1/catalog")

    assert response.status_code == 200
    body = response.json()
    assert body["tag"] == "v1.0.0"
    assert body["stale"] is False
    assert [a["name"] for a in body["apps"]] == ["palmimo-teleop"]


def test_get_catalog_returns_typed_source_and_env_objects(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.catalog.asset = CatalogAsset(tag="v1.0.0", apps=(make_catalog_app("palmimo-teleop"),))
    _authenticated_client(client, adapters)

    response = client.get("/api/v1/catalog")

    assert response.status_code == 200
    [app] = response.json()["apps"]
    assert app["source"] == {
        "type": "git",
        "url": "https://github.com/Jizai-inc/palmimo-devkit",
        "ref": "v1.0.0",
        "ref_kind": "tag",
        "subdir": None,
        "commit": "deadbeef",
        "manifest": None,
        "official": True,
    }
    assert app["env"] == []


def test_get_catalog_exposes_an_envs_help_url(client: TestClient, adapters: FakeAdapterBundle) -> None:
    app = make_catalog_app("palmimo-teleop")
    app = replace(app, env={"TOKEN": CatalogEnvSpec(required=True, description="d", help_url="https://x/help")})
    adapters.catalog.asset = CatalogAsset(tag="v1.0.0", apps=(app,))
    _authenticated_client(client, adapters)

    response = client.get("/api/v1/catalog")

    [env] = response.json()["apps"][0]["env"]
    assert env["help_url"] == "https://x/help"


def test_get_catalog_exposes_a_non_default_manifest_filename(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.catalog.asset = CatalogAsset(
        tag="v1.0.0", apps=(make_catalog_app("palmimo-realtime", manifest="palmimo.realtime.toml"),)
    )
    _authenticated_client(client, adapters)

    response = client.get("/api/v1/catalog")

    [app] = response.json()["apps"]
    assert app["source"]["manifest"] == "palmimo.realtime.toml"


def test_get_catalog_reports_clock_unsynced_when_the_clock_is_not_ntp_synchronized(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    adapters.clock.synchronized = False
    _authenticated_client(client, adapters)

    response = client.get("/api/v1/catalog")

    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is True
    assert body["reason"] == "clock_unsynced"
    assert body["apps"] == []
