"""Tests for ``/api/v1/apps``."""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api.apps import _read_upload_bounded
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core.periodic import run_git_check_sweep
from palmimo_portal.ports import AppRecord, AppSource, AppsState, UpdateJob, UpdateState
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


class _StubUpload:
    """Stands in for Starlette's `UploadFile`: a declared `.size` plus a call-counted `.read`."""

    def __init__(self, size: int | None, chunks: list[bytes]) -> None:
        self.size = size
        self._chunks = list(chunks)
        self.read_calls = 0

    async def read(self, n: int = -1) -> bytes:
        self.read_calls += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


async def test_read_upload_bounded_rejects_an_oversized_declared_size_before_reading() -> None:
    upload = _StubUpload(size=201 * 1024 * 1024, chunks=[b"x" * 1024])

    with pytest.raises(PortalError) as excinfo:
        await _read_upload_bounded(upload, max_bytes=200 * 1024 * 1024)

    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["code"] == "zip_too_large"
    assert upload.read_calls == 0


async def test_read_upload_bounded_aborts_mid_stream_once_the_cap_is_exceeded() -> None:
    # A declared size that undercounts the real body (or none at all) must not let the read
    # loop run unbounded -- this is the fallback the declared-size check above cannot catch.
    upload = _StubUpload(size=None, chunks=[b"x" * 5, b"x" * 5, b"x" * 5])

    with pytest.raises(PortalError) as excinfo:
        await _read_upload_bounded(upload, max_bytes=8)

    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["code"] == "zip_too_large"


async def test_read_upload_bounded_streams_the_body_to_a_file_on_disk_instead_of_memory() -> None:
    upload = _StubUpload(size=10, chunks=[b"abcde", b"fghij"])

    path = await _read_upload_bounded(upload, max_bytes=1024)
    try:
        assert path.read_bytes() == b"abcdefghij"
    finally:
        path.unlink(missing_ok=True)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # apps_run_in_thread=False: runs install/update/delete jobs inline, so
    # assertions below can inspect the result deterministically without
    # waiting on (or racing) a background thread -- see AppsJobRunner.
    return Settings(
        allowed_hosts=frozenset({"testserver"}),
        static_dir=tmp_path / "static-not-built",
        apps_dir=tmp_path / "apps",
        uv_cache_dir=tmp_path / "uv-cache",
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
        archive.writestr("palmimo.toml", f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\n')
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\n")
    return buffer.getvalue()


def _install_zip(client: TestClient, name: str = "palmimo-teleop") -> httpx.Response:
    return client.post(
        "/api/v1/apps/install", files={"file": ("app.zip", _zip_bytes(name), "application/zip")}, headers=CSRF_HEADERS
    )


def _zip_bytes_with_two_manifests() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("palmimo.toml", 'schema = 1\nname = "palmimo-default"\ndescription = "d"\ncommand = ["run"]\n')
        archive.writestr(
            "palmimo.realtime.toml", 'schema = 1\nname = "palmimo-realtime"\ndescription = "d"\ncommand = ["run"]\n'
        )
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\n")
    return buffer.getvalue()


def test_install_zip_with_a_manifest_field_installs_the_app_it_declares(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Without this, a zip shipping several manifests could only ever install whatever app
    # palmimo.toml declares, no matter which manifest the 'manifest' form field named.
    client = _authenticated_client(client, adapters)

    response = client.post(
        "/api/v1/apps/install",
        files={"file": ("app.zip", _zip_bytes_with_two_manifests(), "application/zip")},
        data={"manifest": "palmimo.realtime.toml"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 202
    assert response.json()["job"]["app_name"] == "palmimo-realtime"
    detail = client.get("/api/v1/apps/palmimo-realtime")
    assert detail.status_code == 200
    assert detail.json()["source"]["manifest"] == "palmimo.realtime.toml"


def test_install_zip_then_list_shows_the_app(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    install_response = _install_zip(client)
    assert install_response.status_code == 202
    assert install_response.json()["job"]["state"] == "done"

    list_response = client.get("/api/v1/apps")
    assert list_response.status_code == 200
    [app] = list_response.json()["apps"]
    assert app["name"] == "palmimo-teleop"
    assert app["status"] == "stopped"


def test_list_apps_removes_a_stopped_apps_leftover_run_directory(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A run dir left by a start whose unit already exited carries a secrets-bearing env file --
    # it must not wait for the next Portal restart to be cleaned up.
    client = _authenticated_client(client, adapters)
    _install_zip(client)
    adapters.run_dir.write(
        "palmimo-teleop", env={"API_KEY": "sekrit"}, argv=["run"], cwd="/apps/palmimo-teleop", project="p"
    )

    response = client.get("/api/v1/apps")

    assert response.status_code == 200
    assert "palmimo-teleop" not in adapters.run_dir.written


def test_install_zip_over_existing_name_returns_409(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)

    response = _install_zip(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "app_exists"


def test_install_over_capacity_disk_returns_507(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    adapters.disk.free_bytes_value = 0

    response = _install_zip(client)

    assert response.status_code == 507
    assert response.json()["error"]["code"] == "disk_full"


def test_install_returns_503_platform_not_ready_when_apps_dir_is_missing(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # apps_dir not existing (not yet mounted) is a different failure than the volume being full --
    # os.statvfs raises OSError rather than returning a free-byte count, and that must not surface
    # as a generic 500.
    client = _authenticated_client(client, adapters)
    adapters.disk.raise_on_free_bytes = FileNotFoundError("apps_dir does not exist")

    response = _install_zip(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "platform_not_ready"


def test_install_returns_409_platform_not_ready_when_the_platform_bundle_is_not_ready(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    adapters.platform.installed = None

    response = _install_zip(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "platform_not_ready"
    assert client.get("/api/v1/apps").json()["apps"] == []


def test_install_git_source_records_source_and_commit(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-git-app"\ndescription = "d"\ncommand=["run"]\n')
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    adapters.git.on_clone = seed
    adapters.git.next_commit = "abc123"

    response = client.post(
        "/api/v1/apps/install",
        json={"source": {"type": "git", "url": "https://example.com/repo", "ref": "main", "ref_kind": "branch"}},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 202
    assert response.json()["job"]["state"] == "done"

    detail = client.get("/api/v1/apps/palmimo-git-app")
    assert detail.status_code == 200
    assert detail.json()["source"]["commit"] == "abc123"


@pytest.mark.parametrize(
    "source",
    [
        {"type": "git", "url": "--upload-pack=touch /tmp/pwned", "ref": "main", "ref_kind": "branch"},
        {"type": "git", "url": "http://example.com/repo", "ref": "main", "ref_kind": "branch"},
        {"type": "git", "url": "https://user:pw@example.com/repo", "ref": "main", "ref_kind": "branch"},
        {"type": "git", "url": "https:///no-host", "ref": "main", "ref_kind": "branch"},
        {"type": "git", "url": "https://example.com/repo", "ref": "-x", "ref_kind": "branch"},
        {"type": "git", "url": "https://example.com/repo", "ref": "main; rm -rf /", "ref_kind": "branch"},
        {"type": "git", "url": "https://example.com/repo", "ref": "main", "ref_kind": "branch", "subdir": "../etc"},
        {"type": "git", "url": "https://example.com/repo", "ref": "main", "ref_kind": "branch", "subdir": "/etc"},
    ],
    ids=[
        "url-leading-dash",
        "url-not-https",
        "url-userinfo",
        "url-no-host",
        "ref-leading-dash",
        "ref-shell-metacharacters",
        "subdir-parent-escape",
        "subdir-absolute",
    ],
)
def test_install_git_rejects_unsafe_source_before_any_git_call(
    client: TestClient, adapters: FakeAdapterBundle, source: dict[str, str]
) -> None:
    client = _authenticated_client(client, adapters)

    response = client.post("/api/v1/apps/install", json={"source": source}, headers=CSRF_HEADERS)

    assert response.status_code == 422
    assert adapters.git.clone_calls == []


def test_preview_does_not_install(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.post(
        "/api/v1/apps/preview", files={"file": ("app.zip", _zip_bytes(), "application/zip")}, headers=CSRF_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["name"] == "palmimo-teleop"
    assert client.get("/api/v1/apps").json()["apps"] == []


def test_get_app_returns_404_for_unknown_name(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.get("/api/v1/apps/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "app_not_found"


def test_delete_app_removes_it_from_the_list(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)

    response = client.delete("/api/v1/apps/palmimo-teleop", headers=CSRF_HEADERS)

    assert response.status_code == 202
    assert client.get("/api/v1/apps").json()["apps"] == []


def test_delete_unknown_app_returns_404(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.delete("/api/v1/apps/does-not-exist", headers=CSRF_HEADERS)

    assert response.status_code == 404


def test_put_params_rejects_value_outside_declared_range(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text(
            'schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand = ["run", "{port}"]\n'
            '\n[params.port]\ntype = "int"\nmin = 1024\nmax = 65535\n'
        )
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    adapters.git.on_clone = seed
    client.post(
        "/api/v1/apps/install",
        json={"source": {"type": "git", "url": "https://example.com/repo", "ref": "main", "ref_kind": "branch"}},
        headers=CSRF_HEADERS,
    )

    response = client.put("/api/v1/apps/palmimo-teleop/params", json={"params": {"port": 1}}, headers=CSRF_HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "params_invalid"


def test_put_bindings_to_unregistered_secret_returns_422(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)

    response = client.put(
        "/api/v1/apps/palmimo-teleop/bindings", json={"bindings": {"API_KEY": "NOT_REGISTERED"}}, headers=CSRF_HEADERS
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_secret_name"


def test_update_check_reports_update_available_for_branch_pinned_app(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)

    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand=["run"]\n')
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    adapters.git.on_clone = seed
    adapters.git.next_commit = "v1"
    client.post(
        "/api/v1/apps/install",
        json={"source": {"type": "git", "url": "https://example.com/repo", "ref": "main", "ref_kind": "branch"}},
        headers=CSRF_HEADERS,
    )
    adapters.git.remote_commits[("https://example.com/repo", "main", "branch")] = "v2"

    response = client.post("/api/v1/apps/palmimo-teleop/update/check", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"update_available": True, "remote_commit": "v2"}


def test_update_check_clears_credential_rejected_for_every_app_sharing_the_host_owner(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)

    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand=["run"]\n')
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    adapters.git.on_clone = seed
    adapters.git.next_commit = "v1"
    client.post(
        "/api/v1/apps/install",
        json={"source": {"type": "git", "url": "https://example.com/acme/repo-a", "ref": "main", "ref_kind": "branch"}},
        headers=CSRF_HEADERS,
    )
    adapters.git.remote_commits[("https://example.com/acme/repo-a", "main", "branch")] = "v2"

    state = adapters.state.read_apps_state()
    sibling = AppRecord(
        name="other-app",
        source=AppSource(type="git", url="https://example.com/acme/repo-b", ref="main", ref_kind="branch"),
        installed_at=1.0,
        params={},
        autostart=False,
        last_job=None,
        credential_rejected=True,
    )
    adapters.state.write_apps_state(
        AppsState(
            apps={
                "palmimo-teleop": replace(state.apps["palmimo-teleop"], credential_rejected=True),
                "other-app": sibling,
            }
        )
    )

    response = client.post("/api/v1/apps/palmimo-teleop/update/check", headers=CSRF_HEADERS)

    assert response.status_code == 200
    apps = adapters.state.read_apps_state().apps
    assert apps["palmimo-teleop"].credential_rejected is False
    assert apps["other-app"].credential_rejected is False, "same host/owner credential must also be cleared"


def test_update_app_returns_409_platform_not_ready_when_the_platform_bundle_is_not_ready(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)

    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand=["run"]\n')
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    adapters.git.on_clone = seed
    client.post(
        "/api/v1/apps/install",
        json={"source": {"type": "git", "url": "https://example.com/repo", "ref": "main", "ref_kind": "branch"}},
        headers=CSRF_HEADERS,
    )
    adapters.platform.installed = None

    response = client.post("/api/v1/apps/palmimo-teleop/update", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "platform_not_ready"


def test_get_apps_reflects_a_periodic_sweep_result(
    app: FastAPI, client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)

    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand=["run"]\n')
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    adapters.git.on_clone = seed
    adapters.git.next_commit = "v1"
    client.post(
        "/api/v1/apps/install",
        json={"source": {"type": "git", "url": "https://example.com/repo", "ref": "main", "ref_kind": "branch"}},
        headers=CSRF_HEADERS,
    )
    adapters.git.remote_commits[("https://example.com/repo", "main", "branch")] = "v2"

    run_git_check_sweep(app.state.apps_job_context, adapters.state)

    response = client.get("/api/v1/apps")

    entry = next(a for a in response.json()["apps"] if a["name"] == "palmimo-teleop")
    assert entry["update_available"] is True
    assert entry["latest_commit"] == "v2"


def test_apps_endpoints_reject_when_portal_update_is_in_progress(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    running_job = UpdateJob(
        state="running", kind="update", target="v2", step="sync", error=None, started_at=1.0, finished_at=None
    )
    adapters.state.write_update_state(UpdateState(latest=None, checked_at=None, previous_tag=None, job=running_job))

    response = _install_zip(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_in_progress"


def test_apps_state_corrupt_returns_409_for_list(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    adapters.state.apps_state_corrupt = True

    response = client.get("/api/v1/apps")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "platform_state_corrupt"


def test_get_job_returns_the_recorded_install_job(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    install_response = _install_zip(client)
    job_id = install_response.json()["job"]["id"]

    response = client.get(f"/api/v1/apps/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["state"] == "done"


def test_get_job_returns_404_for_unknown_id(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.get("/api/v1/apps/jobs/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_get_job_reports_the_target_app_and_kind_for_an_install(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    install_response = _install_zip(client)
    job_id = install_response.json()["job"]["id"]

    response = client.get(f"/api/v1/apps/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["app_name"] == "palmimo-teleop"
    assert response.json()["kind"] == "install"


def test_get_app_exposes_manifest_param_specs_for_an_enum_and_a_bounded_numeric_param(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "palmimo.toml",
            'schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand = ["run", "{mode}", "{port}"]\n'
            '\n[params.mode]\ntype = "enum"\nchoices = ["walk", "trot"]\ndefault = "walk"\n'
            '\n[params.port]\ntype = "int"\nmin = 1\nmax = 65535\ndefault = 8080\n',
        )
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\n")
    client.post(
        "/api/v1/apps/install", files={"file": ("app.zip", buffer.getvalue(), "application/zip")}, headers=CSRF_HEADERS
    )

    response = client.get("/api/v1/apps/palmimo-teleop")

    assert response.status_code == 200
    params_by_name = {p["name"]: p for p in response.json()["manifest"]["params"]}
    assert params_by_name["mode"]["choices"] == ["walk", "trot"]
    assert params_by_name["port"]["min"] == 1
    assert params_by_name["port"]["max"] == 65535


def test_get_app_exposes_a_manifest_params_description(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "palmimo.toml",
            'schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand = ["run", "{port}"]\n'
            '\n[params.port]\ntype = "int"\nmin = 1\nmax = 65535\ndefault = 8080\n'
            'description = "TCP port the control server listens on"\n',
        )
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\n")
    client.post(
        "/api/v1/apps/install", files={"file": ("app.zip", buffer.getvalue(), "application/zip")}, headers=CSRF_HEADERS
    )

    response = client.get("/api/v1/apps/palmimo-teleop")

    params_by_name = {p["name"]: p for p in response.json()["manifest"]["params"]}
    assert params_by_name["port"]["description"] == "TCP port the control server listens on"


def test_install_rejects_while_another_apps_job_holds_the_lock(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    with adapters.state.lock_apps():
        response = _install_zip(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "app_job_in_progress"


def _touch_venv(settings: Settings, name: str) -> None:
    venv_bin = settings.apps_dir / name / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "python").write_text("", encoding="utf-8")


def test_start_returns_404_for_an_unknown_app(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.post("/api/v1/apps/does-not-exist/start", headers=CSRF_HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "app_not_found"


def test_start_returns_409_venv_missing_when_the_venv_was_never_synced(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)

    response = client.post("/api/v1/apps/palmimo-teleop/start", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "venv_missing"


def test_start_returns_202_and_starts_the_unit_once_prechecks_pass(
    client: TestClient, adapters: FakeAdapterBundle, settings: Settings
) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)
    _touch_venv(settings, "palmimo-teleop")

    response = client.post("/api/v1/apps/palmimo-teleop/start", headers=CSRF_HEADERS)

    assert response.status_code == 202
    assert response.json() == {"status": "activating"}
    assert adapters.app_unit.start_calls == ["palmimo-teleop"]


def test_start_returns_409_app_running_naming_the_already_active_app(
    client: TestClient, adapters: FakeAdapterBundle, settings: Settings
) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client, "palmimo-teleop")
    _install_zip(client, "palmimo-other")
    _touch_venv(settings, "palmimo-teleop")
    _touch_venv(settings, "palmimo-other")
    client.post("/api/v1/apps/palmimo-other/start", headers=CSRF_HEADERS)

    response = client.post("/api/v1/apps/palmimo-teleop/start", headers=CSRF_HEADERS)

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "app_running"
    assert body["params"]["running"] == "palmimo-other"


def test_start_returns_503_polkit_denied_when_systemd_refuses(
    client: TestClient, adapters: FakeAdapterBundle, settings: Settings
) -> None:
    from palmimo_portal.ports import PolkitDeniedError

    client = _authenticated_client(client, adapters)
    _install_zip(client)
    _touch_venv(settings, "palmimo-teleop")
    adapters.app_unit.raise_on_start = PolkitDeniedError("palmimo-app@palmimo-teleop.service", "start")

    response = client.post("/api/v1/apps/palmimo-teleop/start", headers=CSRF_HEADERS)

    # Distinguishing this from a generic 503 backend failure is the point of the code, not
    # any wording it carries.
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "polkit_denied"


def test_start_returns_500_start_failed_with_journal_tail_on_unexpected_unit_error(
    client: TestClient, adapters: FakeAdapterBundle, settings: Settings
) -> None:
    from palmimo_portal.ports import JournalEntry

    client = _authenticated_client(client, adapters)
    _install_zip(client)
    _touch_venv(settings, "palmimo-teleop")
    adapters.app_unit.raise_on_start = RuntimeError("dbus exploded")
    adapters.journal.entries_by_unit["palmimo-app@palmimo-teleop.service"] = [
        JournalEntry(message="boom", timestamp=1.0, invocation_id="inv-1")
    ]

    response = client.post("/api/v1/apps/palmimo-teleop/start", headers=CSRF_HEADERS)

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "start_failed"
    assert body["params"]["journal_tail"] == ["boom"]


def test_start_failed_journal_tail_masks_a_registered_secret_value(
    client: TestClient, adapters: FakeAdapterBundle, settings: Settings
) -> None:
    from palmimo_portal.ports import JournalEntry

    client = _authenticated_client(client, adapters)
    _install_zip(client)
    _touch_venv(settings, "palmimo-teleop")
    adapters.app_unit.raise_on_start = RuntimeError("dbus exploded")
    adapters.secrets.set_secret("API_KEY", "sk-supersecret")
    adapters.journal.entries_by_unit["palmimo-app@palmimo-teleop.service"] = [
        JournalEntry(message="using key sk-supersecret to authenticate", timestamp=1.0, invocation_id="inv-1")
    ]

    response = client.post("/api/v1/apps/palmimo-teleop/start", headers=CSRF_HEADERS)

    assert response.status_code == 500
    [line] = response.json()["error"]["params"]["journal_tail"]
    assert "sk-supersecret" not in line
    assert "***" in line


def test_logs_masks_a_registered_secret_value(client: TestClient, adapters: FakeAdapterBundle) -> None:
    from palmimo_portal.ports import JournalEntry

    client = _authenticated_client(client, adapters)
    _install_zip(client)
    adapters.secrets.set_secret("API_KEY", "sk-supersecret")
    adapters.journal.entries_by_unit["palmimo-app@palmimo-teleop.service"] = [
        JournalEntry(message="using key sk-supersecret to authenticate", timestamp=1.0, invocation_id="inv-1")
    ]

    response = client.get("/api/v1/apps/palmimo-teleop/logs")

    [entry] = response.json()["entries"]
    assert "sk-supersecret" not in entry["message"]
    assert "***" in entry["message"]


def test_logs_rejects_a_lines_value_above_the_bound(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)

    response = client.get("/api/v1/apps/palmimo-teleop/logs", params={"lines": 5000})

    assert response.status_code == 422


def test_stop_returns_202_and_stops_the_unit(
    client: TestClient, adapters: FakeAdapterBundle, settings: Settings
) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)
    _touch_venv(settings, "palmimo-teleop")
    client.post("/api/v1/apps/palmimo-teleop/start", headers=CSRF_HEADERS)

    response = client.post("/api/v1/apps/palmimo-teleop/stop", headers=CSRF_HEADERS)

    assert response.status_code == 202
    assert response.json() == {"status": "stopping"}
    assert adapters.app_unit.stop_calls == ["palmimo-teleop"]


def test_stop_returns_404_for_an_unknown_app(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.post("/api/v1/apps/does-not-exist/stop", headers=CSRF_HEADERS)

    assert response.status_code == 404


def test_put_autostart_persists_the_flag(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)

    response = client.put("/api/v1/apps/palmimo-teleop/autostart", json={"enabled": True}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json()["autostart"] is True
    assert adapters.state.read_apps_state().apps["palmimo-teleop"].autostart is True


def test_put_source_rejects_a_zip_sourced_app(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)

    response = client.put(
        "/api/v1/apps/palmimo-teleop/source", json={"ref": "v2", "ref_kind": "tag"}, headers=CSRF_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "source_update_git_only"


def test_put_source_updates_ref_for_a_git_sourced_app(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-git-app"\ndescription = "d"\ncommand=["run"]\n')
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    adapters.git.on_clone = seed
    client.post(
        "/api/v1/apps/install",
        json={"source": {"type": "git", "url": "https://example.com/repo", "ref": "main", "ref_kind": "branch"}},
        headers=CSRF_HEADERS,
    )

    response = client.put(
        "/api/v1/apps/palmimo-git-app/source", json={"ref": "v2", "ref_kind": "tag"}, headers=CSRF_HEADERS
    )

    assert response.status_code == 200
    record = adapters.state.read_apps_state().apps["palmimo-git-app"]
    assert record.source.ref == "v2"
    assert record.source.ref_kind == "tag"


def test_logs_returns_journal_permission_when_unreadable(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    _install_zip(client)
    adapters.journal.readable = False

    response = client.get("/api/v1/apps/palmimo-teleop/logs")

    assert response.status_code == 200
    assert response.json()["unavailable"] == "journal_permission"


def test_logs_returns_404_for_an_unknown_app(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.get("/api/v1/apps/does-not-exist/logs")

    assert response.status_code == 404


def test_logs_pages_with_a_cursor(client: TestClient, adapters: FakeAdapterBundle) -> None:
    from palmimo_portal.ports import JournalEntry

    client = _authenticated_client(client, adapters)
    _install_zip(client)
    unit = "palmimo-app@palmimo-teleop.service"
    adapters.journal.entries_by_unit[unit] = [
        JournalEntry(message=f"line {i}", timestamp=float(i), invocation_id="inv-1") for i in range(5)
    ]

    first = client.get("/api/v1/apps/palmimo-teleop/logs", params={"lines": 2})
    assert [entry["message"] for entry in first.json()["entries"]] == ["line 0", "line 1"]
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    second = client.get("/api/v1/apps/palmimo-teleop/logs", params={"lines": 2, "cursor": cursor})
    assert [entry["message"] for entry in second.json()["entries"]] == ["line 2", "line 3"]


def test_logs_filters_by_invocation(client: TestClient, adapters: FakeAdapterBundle) -> None:
    from palmimo_portal.ports import JournalEntry

    client = _authenticated_client(client, adapters)
    _install_zip(client)
    unit = "palmimo-app@palmimo-teleop.service"
    adapters.journal.entries_by_unit[unit] = [
        JournalEntry(message="old", timestamp=1.0, invocation_id="inv-1"),
        JournalEntry(message="new", timestamp=2.0, invocation_id="inv-2"),
    ]

    response = client.get("/api/v1/apps/palmimo-teleop/logs", params={"invocation": "inv-2"})

    assert [entry["message"] for entry in response.json()["entries"]] == ["new"]
    assert response.json()["invocations"] == ["inv-1", "inv-2"]
