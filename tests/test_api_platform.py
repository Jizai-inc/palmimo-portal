"""Tests for ``/api/v1/platform`` and its mutual exclusion with ``/api/v1/update`` and ``/api/v1/apps``."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.ports import PlatformJob, PlatformUpdateState, Release, ReleaseSourceError, UpdateJob, UpdateState
from palmimo_portal.settings import DEFAULT_REQUIRED_PLATFORM_VERSION, Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}

READY_MANIFEST = {
    "version": DEFAULT_REQUIRED_PLATFORM_VERSION,
    "requires_portal": "0.0.0",
    "restart_portal": False,
    "summary": "adds a device class",
    "reflash_required": False,
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # apps_run_in_thread=False also drives PlatformJobRunner inline (api/app.py
    # reuses the same flag) so assertions below see a job's final state directly.
    return Settings(
        allowed_hosts=frozenset({"testserver"}),
        static_dir=tmp_path / "static-not-built",
        apps_dir=tmp_path / "apps",
        uv_cache_dir=tmp_path / "uv-cache",
        platform_dir=tmp_path / "platform",
        apps_run_in_thread=False,
    )


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


def _set_available_release(adapters: FakeAdapterBundle, *, manifest: dict[str, object] | None = None) -> None:
    adapters.platform_releases.latest = Release(
        tag="v2", name="v2", published_at="2026-01-01T00:00:00Z", html_url="https://example.test/v2"
    )
    adapters.platform_bundle.manifest = dict(manifest or READY_MANIFEST)


def _zip_bytes(name: str = "palmimo-teleop") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("palmimo.toml", f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\n')
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\n")
    return buffer.getvalue()


def _install_app(client: TestClient, name: str = "palmimo-teleop") -> None:
    response = client.post(
        "/api/v1/apps/install", files={"file": ("app.zip", _zip_bytes(name), "application/zip")}, headers=CSRF_HEADERS
    )
    assert response.status_code == 202, response.text


def test_get_platform_requires_auth(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.known_networks.add("home")

    response = client.get("/api/v1/platform")

    assert response.status_code == 401


def test_get_platform_reports_ready_when_installed_meets_the_required_version(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)

    response = client.get("/api/v1/platform")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["installed_version"] == DEFAULT_REQUIRED_PLATFORM_VERSION  # FakePlatformPort's default


def test_get_platform_reports_not_ready_when_no_bundle_is_installed(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    adapters.platform.installed = None

    response = client.get("/api/v1/platform")

    body = response.json()
    assert body["ready"] is False
    assert body["reason"] == "platform_not_installed"


def test_get_platform_reports_clock_unsynced_latest_error_without_fetching(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    _set_available_release(adapters)
    adapters.clock.synchronized = False

    response = client.get("/api/v1/platform")

    assert response.status_code == 200
    body = response.json()
    assert body["latest"] is None
    assert body["latest_error"] == "clock_unsynced"
    assert adapters.platform_releases.fetch_calls == 0


def test_post_platform_check_is_rate_limited_within_a_minute_of_the_last_check(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    _set_available_release(adapters)
    first = client.post("/api/v1/platform/check", headers=CSRF_HEADERS)
    assert first.status_code == 200

    response = client.post("/api/v1/platform/check", headers=CSRF_HEADERS)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "platform_check_rate_limited"
    assert response.json()["error"]["params"]["retry_after_seconds"] > 0


def test_post_platform_check_maps_a_fetch_failure_the_same_way_get_does(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    adapters.platform_releases.raise_on_fetch = ReleaseSourceError("release_source_unavailable", "DNS failure")

    get_response = client.get("/api/v1/platform")
    check_response = client.post("/api/v1/platform/check", headers=CSRF_HEADERS)

    assert check_response.status_code == 200
    assert check_response.json()["latest"] is None
    assert check_response.json()["latest_error"] == get_response.json()["latest_error"]


def test_post_platform_update_runs_the_full_pipeline_and_flips_readiness(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    adapters.platform.installed = None
    adapters.platform.next_version = DEFAULT_REQUIRED_PLATFORM_VERSION
    _set_available_release(adapters)

    response = client.post("/api/v1/platform/update", headers=CSRF_HEADERS)

    assert response.status_code == 202
    assert response.json()["job"]["state"] == "done"
    status = client.get("/api/v1/platform").json()
    assert status["ready"] is True
    assert status["installed_version"] == DEFAULT_REQUIRED_PLATFORM_VERSION


def test_platform_readiness_gates_app_install_and_start(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # This is the app-platform vertical's contract with the app-runtime feature
    # (design doc 2.7): install/start must refuse until the platform is ready.
    client = _authenticated_client(client, adapters)
    _install_app(client)
    adapters.platform.installed = None

    response = client.post("/api/v1/apps/palmimo-teleop/start", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "platform_not_ready"


def test_post_platform_update_returns_409_while_a_portal_self_update_is_running(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    _set_available_release(adapters)
    adapters.state.write_update_state(
        UpdateState(
            latest=None,
            checked_at=None,
            previous_tag=None,
            job=UpdateJob(
                state="running", kind="update", target="v9", step=None, error=None, started_at=1.0, finished_at=None
            ),
        )
    )

    response = client.post("/api/v1/platform/update", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_in_progress"


def test_post_platform_update_returns_409_while_an_apps_job_holds_the_lock(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    _set_available_release(adapters)
    lock_cm = adapters.state.lock_apps()
    lock_cm.__enter__()
    try:
        response = client.post("/api/v1/platform/update", headers=CSRF_HEADERS)
    finally:
        lock_cm.__exit__(None, None, None)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "app_job_in_progress"


def test_app_install_returns_409_while_a_platform_update_job_is_running(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    adapters.state.write_platform_update_state(
        PlatformUpdateState(
            job=PlatformJob(
                state="running", target_version=2, step="install", error=None, started_at=1.0, finished_at=None
            )
        )
    )

    response = client.post(
        "/api/v1/apps/install", files={"file": ("app.zip", b"", "application/zip")}, headers=CSRF_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "platform_update_in_progress"


def test_portal_self_update_returns_409_while_a_platform_update_job_is_running(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    adapters.state.write_platform_update_state(
        PlatformUpdateState(
            job=PlatformJob(
                state="running", target_version=2, step="install", error=None, started_at=1.0, finished_at=None
            )
        )
    )
    adapters.releases.latest = Release(
        tag="v9", name="v9", published_at="2026-01-01T00:00:00Z", html_url="https://example.test/v9"
    )
    adapters.state.write_update_state(
        UpdateState(
            latest=adapters.releases.latest,
            checked_at=1.0,
            previous_tag=None,
            job=UpdateJob(
                state="idle", kind="update", target=None, step=None, error=None, started_at=None, finished_at=None
            ),
        )
    )

    response = client.post("/api/v1/update/apply", json={"tag": "v9"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "platform_update_in_progress"


def test_post_platform_update_returns_409_when_the_job_is_running_despite_a_free_lock(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Defense-in-depth: `start_update` (core/platform_update.py) also refuses a persisted
    # "running" job even when `platform.lock` itself is free -- this must 409, not 500.
    client = _authenticated_client(client, adapters)
    _set_available_release(adapters)
    adapters.state.write_platform_update_state(
        PlatformUpdateState(
            job=PlatformJob(
                state="running", target_version=2, step="install", error=None, started_at=1.0, finished_at=None
            )
        )
    )

    response = client.post("/api/v1/platform/update", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "platform_update_in_progress"


def test_startup_finalize_marks_an_interrupted_platform_job_as_failed(
    app: FastAPI, adapters: FakeAdapterBundle
) -> None:
    adapters.state.write_platform_update_state(
        PlatformUpdateState(
            job=PlatformJob(
                state="running", target_version=2, step="install", error=None, started_at=1.0, finished_at=None
            )
        )
    )

    with TestClient(app) as client:
        client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
        adapters.network.known_networks.add("home")
        client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
        response = client.get("/api/v1/platform/update")

    job = response.json()["job"]
    assert job["state"] == "failed"
    assert job["step"] == "install"
