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
    platform_dir = tmp_path / "platform"
    current = platform_dir / "current"
    current.mkdir(parents=True)
    (current / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return Settings(
        allowed_hosts=frozenset({"testserver"}),
        static_dir=tmp_path / "static-not-built",
        apps_dir=tmp_path / "apps",
        uv_cache_dir=tmp_path / "uv-cache",
        platform_dir=platform_dir,
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
    adapters.saved_networks.save("home", "", first=False)
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
    adapters.saved_networks.save("home", "", first=False)

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


def test_get_platform_reports_verify_unavailable_when_startup_verify_could_not_run(
    app: FastAPI, client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    app.state.platform_verify_diffs = None

    response = client.get("/api/v1/platform")

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["reason"] == "platform_verify_unavailable"


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
    retry_after = response.json()["error"]["params"]["retry_after_seconds"]
    assert isinstance(retry_after, int) and retry_after > 0


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


def test_legacy_ledger_blocks_platform_check_and_update_until_apps_are_reset(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    _set_available_release(adapters)
    adapters.state.apps_state_legacy = True

    check_response = client.post("/api/v1/platform/check", headers=CSRF_HEADERS)
    update_response = client.post("/api/v1/platform/update", headers=CSRF_HEADERS)

    assert check_response.status_code == 409
    assert check_response.json()["error"]["code"] == "ledger_legacy"
    assert update_response.status_code == 409
    assert update_response.json()["error"]["code"] == "ledger_legacy"
    assert adapters.platform.install_calls == []

    reset_response = client.post("/api/v1/apps/reset", headers=CSRF_HEADERS)
    update_response = client.post("/api/v1/platform/update", headers=CSRF_HEADERS)

    assert reset_response.status_code == 202
    assert update_response.status_code == 202
    assert len(adapters.platform.install_calls) == 1


def test_platform_readiness_gates_app_install_and_start(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # This is the app-platform vertical's contract with the app-runtime feature
    # (design doc 2.7): install/start must refuse until the platform is ready.
    client = _authenticated_client(client, adapters)
    _install_app(client)
    adapters.platform.installed = None

    response = client.post("/api/v1/apps/zip.palmimo-teleop/start", headers=CSRF_HEADERS)

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
        adapters.saved_networks.save("home", "", first=False)
        client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
        response = client.get("/api/v1/platform/update")

    job = response.json()["job"]
    assert job["state"] == "failed"
    assert job["step"] == "install"


def test_manual_display_firmware_job_reports_result(
    client: TestClient,
    adapters: FakeAdapterBundle,
) -> None:
    client = _authenticated_client(client, adapters)
    adapters.platform.display_firmware_available = True
    response = client.post("/api/v1/platform/display-firmware", headers=CSRF_HEADERS)
    assert response.status_code == 202
    assert response.json()["job"]["state"] == "done"
    assert response.json()["display_firmware"]["result"] == "current"
    assert adapters.platform.display_firmware_calls == 1
    assert adapters.platform.install_calls == []
    assert client.get("/api/v1/platform/update").json()["display_firmware"]["result"] == "current"


@pytest.mark.parametrize("blocker", ["platform", "portal", "apps"])
def test_manual_display_firmware_refuses_conflicting_job(
    client: TestClient,
    adapters: FakeAdapterBundle,
    blocker: str,
) -> None:
    from contextlib import nullcontext

    client = _authenticated_client(client, adapters)
    adapters.platform.display_firmware_available = True
    if blocker == "platform":
        adapters.state.write_platform_update_state(
            PlatformUpdateState(
                job=PlatformJob("running", 2, "install", None, 1, None),
            )
        )
    if blocker == "portal":
        adapters.state.write_update_state(
            UpdateState(
                latest=None,
                checked_at=None,
                previous_tag=None,
                job=UpdateJob(
                    state="running", kind="update", target="v2", step="sync", error=None, started_at=1, finished_at=None
                ),
            )
        )
    with adapters.state.lock_apps() if blocker == "apps" else nullcontext():
        response = client.post("/api/v1/platform/display-firmware", headers=CSRF_HEADERS)
    assert response.status_code == 409
    assert adapters.platform.display_firmware_calls == 0


def test_platform_status_exposes_tool_state(
    client: TestClient,
    adapters: FakeAdapterBundle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from palmimo_portal.adapters.platform import SudoPlatformPort

    client = _authenticated_client(client, adapters)
    path = tmp_path / "firmware.json"
    path.write_text(
        json.dumps(
            {
                "seen": True,
                "chip_id": "private-device-id",
                "declared_version": "1.0.0",
                "consecutive_failures": 2,
                "last_result": "failed",
                "last_reason": "missing",
                "at": "2026-10-26T00:00:00Z",
            }
        )
    )
    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", display_firmware_state_path=path)
    monkeypatch.setattr(adapters.platform, "read_display_firmware_state", port.read_display_firmware_state)
    assert client.get("/api/v1/platform").json()["display_firmware"] == {
        "last_result": "failed",
        "last_reason": "missing",
    }


def test_display_firmware_job_holds_app_locks_and_blocks_power_and_updates(
    app: FastAPI,
    client: TestClient,
    adapters: FakeAdapterBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from palmimo_portal.core.platform import PlatformJobRunner
    from palmimo_portal.ports import DisplayFirmwareResult

    client = _authenticated_client(client, adapters)
    _install_app(client)
    app_id = next(iter(adapters.state.read_apps_state().apps))
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def ensure() -> DisplayFirmwareResult:
        entered.set()
        assert release.wait(5)
        return DisplayFirmwareResult(result="updated", before="legacy", after="1.0.0")

    def completed(state: PlatformUpdateState) -> None:
        original_write(state)
        if state.job.state == "done":
            finished.set()

    original_write = adapters.state.write_platform_update_state
    monkeypatch.setattr(adapters.state, "write_platform_update_state", completed)
    monkeypatch.setattr(adapters.platform, "ensure_display_firmware", ensure)
    adapters.platform.display_firmware_available = True
    app.state.platform_job_runner = PlatformJobRunner(adapters.state, app.state.platform_update_runner)
    try:
        response = client.post("/api/v1/platform/display-firmware", headers=CSRF_HEADERS)
        assert response.status_code == 202
        assert entered.wait(5)
        assert client.get("/api/v1/platform/update").json()["job"]["step"] == "display_firmware"
        for path in ("system/reboot", "system/shutdown", "platform/display-firmware"):
            assert client.post(f"/api/v1/{path}", headers=CSRF_HEADERS).status_code == 409
        assert client.post("/api/v1/update/apply", json={"tag": "v2"}, headers=CSRF_HEADERS).status_code == 409
        for action, code in (("start", "app_job_in_progress"), ("stop", "app_busy")):
            response = client.post(f"/api/v1/apps/{app_id}/{action}", headers=CSRF_HEADERS)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == code
    finally:
        release.set()
        assert finished.wait(5)
    state = adapters.state.read_platform_update_state()
    assert state.job.state == "done"
    assert state.display_firmware == DisplayFirmwareResult(result="updated", before="legacy", after="1.0.0")


@pytest.mark.parametrize(
    "operation", ["system/reboot", "system/shutdown", "platform/display-firmware", "platform/update"]
)
def test_dead_display_firmware_job_does_not_block_recovery(
    app: FastAPI,
    adapters: FakeAdapterBundle,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        client = _authenticated_client(client, adapters)
        adapters.platform.display_firmware_available = True
        _set_available_release(adapters)
        original_write = adapters.state.write_platform_update_state
        fail_once = True

        def write(state: PlatformUpdateState) -> None:
            nonlocal fail_once
            if state.job.state == "done" and fail_once:
                fail_once = False
                raise OSError("disk full")
            original_write(state)

        monkeypatch.setattr(adapters.state, "write_platform_update_state", write)
        assert client.post("/api/v1/platform/display-firmware", headers=CSRF_HEADERS).status_code == 500
        assert adapters.state.read_platform_update_state().job.state == "running"
        response = client.post(f"/api/v1/{operation}", headers=CSRF_HEADERS)
        assert response.status_code in (200, 202), response.text
        assert client.get("/api/v1/platform/update").json()["job"]["state"] == "done"


def test_manual_display_firmware_refuses_missing_tool(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    response = client.post("/api/v1/platform/display-firmware", headers=CSRF_HEADERS)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "display_firmware_unavailable"
    assert adapters.state.read_platform_update_state().job.state == "idle"
