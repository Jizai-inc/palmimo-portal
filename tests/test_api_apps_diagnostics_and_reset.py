"""Tests for `GET /apps/{name}/diagnostics` and `POST /apps/reset` (design doc 3.2)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.core.apps_diagnostics import SECTIONS
from palmimo_portal.ports import PlatformJob, PlatformUpdateState, UpdateJob, UpdateState
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        allowed_hosts=frozenset({"testserver"}),
        static_dir=tmp_path / "static-not-built",
        apps_dir=tmp_path / "apps",
        uv_cache_dir=tmp_path / "uv-cache",
        secrets_dir=tmp_path / "secrets",
        apps_run_in_thread=False,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    from palmimo_portal.api.app import create_app

    return create_app(settings)


def _authenticated_client(client: TestClient, adapters: FakeAdapterBundle) -> TestClient:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    return client


def _zip_bytes(name: str = "palmimo-teleop") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "palmimo.toml",
            f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\n'
            '\n[env.API_KEY]\ndescription = "a key"\n',
        )
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\n")
    return buffer.getvalue()


def _install_zip(client: TestClient, name: str = "palmimo-teleop") -> httpx.Response:
    return client.post(
        "/api/v1/apps/install", files={"file": ("app.zip", _zip_bytes(name), "application/zip")}, headers=CSRF_HEADERS
    )


def _touch_venv(settings: Settings, name: str) -> None:
    venv_bin = settings.apps_dir / name / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "python").write_text("", encoding="utf-8")


def test_diagnostics_contains_every_fixed_section_header(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)

    response = client.get("/api/v1/apps/zip.palmimo-teleop/diagnostics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for section in SECTIONS:
        assert section in response.text


def test_diagnostics_returns_404_for_an_unknown_app(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.get("/api/v1/apps/does-not-exist/diagnostics")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "app_not_found"


def test_diagnostics_never_leaks_a_registered_secret_value_or_git_token(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)
    client.put("/api/v1/secrets/MY_KEY", json={"value": "sekrit-value-123"}, headers=CSRF_HEADERS)
    client.put(
        "/api/v1/apps/zip.palmimo-teleop/bindings", json={"bindings": {"API_KEY": "MY_KEY"}}, headers=CSRF_HEADERS
    )
    client.put("/api/v1/git-credentials/github.com/x", json={"value": "ghp_topsecrettoken"}, headers=CSRF_HEADERS)
    adapters.journal.entries_by_unit["palmimo-app@zip.palmimo-teleop.service"] = []
    from palmimo_portal.ports import JournalEntry

    adapters.journal.entries_by_unit["palmimo-app@zip.palmimo-teleop.service"] = [
        JournalEntry(
            message="printed sekrit-value-123 and ghp_topsecrettoken by mistake", timestamp=1.0, invocation_id="i1"
        )
    ]

    response = client.get("/api/v1/apps/zip.palmimo-teleop/diagnostics")

    assert response.status_code == 200
    assert "sekrit-value-123" not in response.text
    assert "ghp_topsecrettoken" not in response.text


@pytest.mark.parametrize(
    ("bind_secret", "expected_start_status", "expected_code"),
    [(True, 202, "ok"), (False, 409, "env_unbound")],
)
def test_diagnostics_precheck_section_matches_what_start_would_answer(
    client: TestClient,
    adapters: FakeAdapterBundle,
    settings: Settings,
    bind_secret: bool,
    expected_start_status: int,
    expected_code: str,
) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)
    _touch_venv(settings, "zip.palmimo-teleop")
    if bind_secret:
        client.put("/api/v1/secrets/MY_KEY", json={"value": "v"}, headers=CSRF_HEADERS)
        client.put(
            "/api/v1/apps/zip.palmimo-teleop/bindings", json={"bindings": {"API_KEY": "MY_KEY"}}, headers=CSRF_HEADERS
        )

    diagnostics = client.get("/api/v1/apps/zip.palmimo-teleop/diagnostics")
    start = client.post("/api/v1/apps/zip.palmimo-teleop/start", headers=CSRF_HEADERS)

    assert start.status_code == expected_start_status
    if expected_code == "ok":
        assert "[precheck]\nok" in diagnostics.text
    else:
        assert f"code={expected_code}" in diagnostics.text
        assert start.json()["error"]["code"] == expected_code


def test_reset_stops_running_apps_removes_them_and_empties_secrets(
    client: TestClient, adapters: FakeAdapterBundle, settings: Settings
) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)
    _touch_venv(settings, "zip.palmimo-teleop")
    client.put("/api/v1/secrets/MY_KEY", json={"value": "v"}, headers=CSRF_HEADERS)
    client.put(
        "/api/v1/apps/zip.palmimo-teleop/bindings", json={"bindings": {"API_KEY": "MY_KEY"}}, headers=CSRF_HEADERS
    )
    start = client.post("/api/v1/apps/zip.palmimo-teleop/start", headers=CSRF_HEADERS)
    assert start.status_code == 202
    assert "zip.palmimo-teleop" in adapters.app_unit.start_calls

    response = client.post("/api/v1/apps/reset", headers=CSRF_HEADERS)

    assert response.status_code == 202
    assert "zip.palmimo-teleop" in adapters.app_unit.stop_calls
    assert client.get("/api/v1/apps").json()["apps"] == []
    assert client.get("/api/v1/secrets").json()["secrets"] == []


def test_reset_leaves_the_catalog_cache(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    from palmimo_portal.ports import CatalogCacheState

    adapters.state.write_catalog_cache(CatalogCacheState(tag="v1.0.0", apps=(), fetched_at=1.0))

    client.post("/api/v1/apps/reset", headers=CSRF_HEADERS)

    assert adapters.state.read_catalog_cache().tag == "v1.0.0"


def test_reset_returns_409_while_an_apps_job_is_running(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    with adapters.state.lock_apps():
        response = client.post("/api/v1/apps/reset", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "app_job_in_progress"


def test_reset_returns_409_while_run_lock_is_held(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    with adapters.state.lock_run():
        response = client.post("/api/v1/apps/reset", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "app_busy"


def test_reset_returns_409_while_a_portal_update_is_running(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    running_job = UpdateJob(
        state="running", kind="update", target="v2", step="sync", error=None, started_at=1.0, finished_at=None
    )
    adapters.state.write_update_state(UpdateState(latest=None, checked_at=None, previous_tag=None, job=running_job))

    response = client.post("/api/v1/apps/reset", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_in_progress"


def test_install_bind_start_and_stop_never_log_a_registered_secret_value(
    client: TestClient, adapters: FakeAdapterBundle, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="palmimo_portal")
    client = _authenticated_client(client, adapters)
    _install_zip(client)
    _touch_venv(settings, "zip.palmimo-teleop")
    client.put("/api/v1/secrets/MY_KEY", json={"value": "sekrit-value-999"}, headers=CSRF_HEADERS)
    client.put(
        "/api/v1/apps/zip.palmimo-teleop/bindings", json={"bindings": {"API_KEY": "MY_KEY"}}, headers=CSRF_HEADERS
    )
    client.post("/api/v1/apps/zip.palmimo-teleop/start", headers=CSRF_HEADERS)
    client.post("/api/v1/apps/zip.palmimo-teleop/stop", headers=CSRF_HEADERS)

    all_log_text = "\n".join(record.getMessage() for record in caplog.records)

    assert "sekrit-value-999" not in all_log_text


def test_reset_returns_409_while_a_platform_update_is_running(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    adapters.state.write_platform_update_state(
        PlatformUpdateState(
            job=PlatformJob(
                state="running", target_version=2, step="install", error=None, started_at=1.0, finished_at=None
            )
        )
    )

    response = client.post("/api/v1/apps/reset", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "platform_update_in_progress"
