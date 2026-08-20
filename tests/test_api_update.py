"""Tests for ``/api/v1/update``."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.core.auth import hash_password
from palmimo_portal.ports import (
    AdapterUnavailableError,
    Identity,
    InstalledVersion,
    Release,
    ReleaseSourceError,
    UpdateJob,
    UpdateState,
)
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}

RELEASE_V2 = Release(
    tag="v2.0.0", name="v2.0.0", published_at="2026-01-01T00:00:00Z", html_url="https://example.test/v2"
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # update_run_in_thread=False: drives the fake updater synchronously so
    # the assertions below can inspect the state left behind without
    # waiting on (or racing) a background thread. update_restart_delay_seconds=0:
    # otherwise every apply/rollback test in this module would block for the
    # real restart delay (see UpdateRunner's docstring).
    return Settings(
        allowed_hosts=frozenset({"testserver"}),
        static_dir=tmp_path / "static-not-built",
        update_run_in_thread=False,
        update_restart_delay_seconds=0.0,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    from palmimo_portal.api.app import create_app

    return create_app(settings)


@pytest.fixture
def adapters(app: FastAPI) -> FakeAdapterBundle:
    bundle: FakeAdapterBundle = app.state.adapters
    return bundle


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _log_in(client: TestClient, adapters: FakeAdapterBundle) -> None:
    """Provision, set a password, connect Wi-Fi, and log in -- the update router's full gate."""
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)


# -- status -----------------------------------------------------------------------------------


def test_status_requires_auth(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.known_networks.add("home")  # provisioned, but not logged in

    response = client.get("/api/v1/update/status")

    assert response.status_code == 401


def test_status_requires_provisioning(client: TestClient) -> None:
    response = client.get("/api/v1/update/status")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_provisioned"


def test_status_reports_installed_and_no_latest_by_default(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)

    response = client.get("/api/v1/update/status")

    assert response.status_code == 200
    body = response.json()
    assert body["latest"] is None
    assert body["update_available"] is False
    assert body["job"]["state"] == "idle"
    assert body["previous_tag"] is None


def test_status_reports_update_available_once_a_newer_release_is_recorded(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _log_in(client, adapters)
    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="abc")
    adapters.releases.latest = RELEASE_V2

    client.post("/api/v1/update/check", headers=CSRF_HEADERS)
    response = client.get("/api/v1/update/status")

    body = response.json()
    assert body["update_available"] is True
    assert body["latest"]["tag"] == "v2.0.0"


def test_status_fails_a_restarting_job_stuck_past_the_max_age(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # A restart that never actually happens (systemd never brought the
    # process back up) must not leave every poll of this endpoint showing
    # "restarting" forever. started_at=1.0 is far past
    # DEFAULT_RESTART_MAX_AGE_SECONDS (600s) from "now".
    _log_in(client, adapters)
    adapters.state.write_update_state(
        UpdateState(
            latest=RELEASE_V2,
            checked_at=100.0,
            previous_tag=None,
            job=UpdateJob(
                state="restarting",
                kind="update",
                target="v2.0.0",
                step=None,
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )

    response = client.get("/api/v1/update/status")

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["state"] == "failed"
    assert body["job"]["step"] == "restart"
    assert "reboot from the Power screen" in body["job"]["error"]
    # Persisted, not just reported on this one response.
    assert adapters.state.read_update_state().job.state == "failed"


def test_status_leaves_a_fresh_restarting_job_alone(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.state.write_update_state(
        UpdateState(
            latest=RELEASE_V2,
            checked_at=100.0,
            previous_tag=None,
            job=UpdateJob(
                state="restarting",
                kind="update",
                target="v2.0.0",
                step=None,
                error=None,
                started_at=time.time(),
                finished_at=None,
            ),
        )
    )

    response = client.get("/api/v1/update/status")

    assert response.status_code == 200
    assert response.json()["job"]["state"] == "restarting"


def test_status_fails_a_running_job_when_no_runner_is_alive(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # A write-failure-killed runner thread (e.g. a full disk) leaves
    # update.json stuck "running" forever -- must not wedge every future
    # check/apply/rollback with 409 update_in_progress indefinitely. Age
    # plays no role any more: even a job written a moment ago is expired
    # once nothing in this process is actually alive and working on it --
    # started_at is set to a recent-looking value deliberately, to prove
    # that.
    _log_in(client, adapters)
    adapters.state.write_update_state(
        UpdateState(
            latest=RELEASE_V2,
            checked_at=100.0,
            previous_tag=None,
            job=UpdateJob(
                state="running",
                kind="update",
                target="v2.0.0",
                step="sync",
                error=None,
                started_at=time.time(),
                finished_at=None,
            ),
        )
    )

    response = client.get("/api/v1/update/status")

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["state"] == "failed"
    assert body["job"]["step"] == "sync"
    # Persisted, not just reported on this one response.
    assert adapters.state.read_update_state().job.state == "failed"
    # And the state machine is genuinely unstuck: a new check can start.
    adapters.releases.latest = RELEASE_V2
    check_response = client.post("/api/v1/update/check", headers=CSRF_HEADERS)
    assert check_response.status_code == 200


def test_status_leaves_a_running_job_alone_while_the_runner_is_alive(
    client: TestClient, adapters: FakeAdapterBundle, app: FastAPI
) -> None:
    # Liveness, not age, is what protects a "running" job now -- started_at
    # is deliberately old here, to prove a legitimately slow apply
    # (uv sync rebuilding wheels) is never expired out from under a runner
    # that is still actually working on it.
    _log_in(client, adapters)
    adapters.state.write_update_state(
        UpdateState(
            latest=RELEASE_V2,
            checked_at=100.0,
            previous_tag=None,
            job=UpdateJob(
                state="running",
                kind="update",
                target="v2.0.0",
                step="sync",
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )
    app.state.update_runner_alive.set()

    response = client.get("/api/v1/update/status")

    assert response.status_code == 200
    assert response.json()["job"]["state"] == "running"


# -- check ------------------------------------------------------------------------------------


def test_check_happy_path_records_the_release(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.releases.latest = RELEASE_V2

    response = client.post("/api/v1/update/check", headers=CSRF_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["latest"]["tag"] == "v2.0.0"
    assert body["checked_at"] is not None
    assert body["job"]["state"] == "idle"


def test_check_maps_no_release_to_a_404(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.releases.latest = None  # FakeReleaseSource raises ReleaseSourceError("no_release", ...)

    response = client.post("/api/v1/update/check", headers=CSRF_HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_release"


def test_check_maps_a_generic_source_failure_to_a_502(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.releases.raise_on_fetch = ReleaseSourceError("release_source_unavailable", "DNS failure")

    response = client.post("/api/v1/update/check", headers=CSRF_HEADERS)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "release_source_unavailable"


def test_check_is_rate_limited_within_a_minute_of_the_last_check(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _log_in(client, adapters)
    adapters.releases.latest = RELEASE_V2
    first = client.post("/api/v1/update/check", headers=CSRF_HEADERS)
    assert first.status_code == 200

    response = client.post("/api/v1/update/check", headers=CSRF_HEADERS)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "update_check_rate_limited"
    assert response.json()["error"]["params"]["retry_after_seconds"] > 0


def test_check_conflicts_with_an_in_progress_job(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.state.write_update_state(
        UpdateState(
            latest=None,
            checked_at=None,
            previous_tag=None,
            job=UpdateJob(
                state="running",
                kind="update",
                target="v2.0.0",
                step="fetch",
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )

    response = client.post("/api/v1/update/check", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_in_progress"


# -- apply ------------------------------------------------------------------------------------


def _check_v2(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.releases.latest = RELEASE_V2
    response = client.post("/api/v1/update/check", headers=CSRF_HEADERS)
    assert response.status_code == 200


def test_apply_happy_path_ends_restarting_and_restarts_the_service(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _log_in(client, adapters)
    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="abc")
    _check_v2(client, adapters)

    response = client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["job"]["state"] == "restarting"
    assert body["job"]["kind"] == "update"
    assert body["job"]["target"] == "v2.0.0"
    assert body["previous_tag"] == "v1.0.0"
    assert adapters.system.restart_calls == 1
    assert adapters.updater.apply_calls == ["v2.0.0"]


def test_apply_rejects_a_mismatched_tag(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    _check_v2(client, adapters)

    response = client.post("/api/v1/update/apply", json={"tag": "v9.9.9"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_target_mismatch"
    assert adapters.updater.apply_calls == []


def test_apply_rejects_when_no_release_was_ever_checked(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)

    response = client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_release_checked"


def test_apply_returns_500_and_writes_nothing_when_update_json_cannot_be_written(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _log_in(client, adapters)
    _check_v2(client, adapters)
    before = adapters.state.read_update_state()
    adapters.state.raise_on_write_update_state = OSError("no space left on device")

    with pytest.raises(OSError, match="no space left on device"):
        client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)

    adapters.state.raise_on_write_update_state = None
    assert adapters.state.read_update_state() == before
    assert adapters.updater.apply_calls == []  # the runner was never started


def test_apply_rejects_while_a_job_is_already_in_progress(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    _check_v2(client, adapters)
    adapters.state.write_update_state(
        UpdateState(
            latest=RELEASE_V2,
            checked_at=1.0,
            previous_tag=None,
            job=UpdateJob(
                state="running",
                kind="update",
                target="v2.0.0",
                step="fetch",
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )

    response = client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_in_progress"


def test_apply_failure_at_sync_marks_the_job_failed_and_keeps_previous_tag(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _log_in(client, adapters)
    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="abc")
    _check_v2(client, adapters)
    adapters.updater.fail_at_step = "sync"
    adapters.updater.fail_message = "uv sync failed: dependency conflict"

    response = client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["job"]["state"] == "failed"
    assert body["job"]["step"] == "sync"
    assert "dependency conflict" in body["job"]["error"]
    assert body["previous_tag"] == "v1.0.0"
    assert adapters.system.restart_calls == 0


def test_apply_restart_failure_marks_the_job_failed_with_a_manual_reboot_hint(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _log_in(client, adapters)
    _check_v2(client, adapters)
    adapters.system.raise_on_restart_portal = AdapterUnavailableError("system_backend_unavailable", "logind down")

    response = client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)

    body = response.json()
    assert body["job"]["state"] == "failed"
    assert body["job"]["step"] == "restart"
    assert "Power screen" in body["job"]["error"]


# -- rollback -----------------------------------------------------------------------------------


def test_rollback_requires_a_previous_tag(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)

    response = client.post("/api/v1/update/rollback", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_previous_version"


def test_rollback_happy_path_targets_the_previous_tag(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="abc")
    _check_v2(client, adapters)
    client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)
    # finalize as if the restart landed on v2.0.0, freeing the job back to idle/done.
    adapters.updater.installed_version = InstalledVersion(tag="v2.0.0", commit="def")
    from palmimo_portal.core.update import finalize_after_restart

    state = adapters.state.read_update_state()
    adapters.state.write_update_state(finalize_after_restart(state, adapters.updater.installed(), now=1000.0))

    response = client.post("/api/v1/update/rollback", headers=CSRF_HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["job"]["kind"] == "rollback"
    assert body["job"]["target"] == "v1.0.0"
    assert adapters.updater.apply_calls == ["v2.0.0", "v1.0.0"]


def test_rollback_of_a_rollback_targets_the_tag_that_was_left(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # apply v1.0.0 -> v2.0.0, finalize, rollback v2.0.0 -> v1.0.0, finalize,
    # rollback again -- start_rollback's own docstring says the second
    # rollback should target v2.0.0 (the tag being left the first time),
    # not v1.0.0 again. See core/update.py's start_rollback docstring and
    # matrix row "rollback of a rollback" in the failure-mode audit.
    from palmimo_portal.core.update import finalize_after_restart

    _log_in(client, adapters)
    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="abc")
    _check_v2(client, adapters)
    client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)
    adapters.updater.installed_version = InstalledVersion(tag="v2.0.0", commit="def")
    state = adapters.state.read_update_state()
    adapters.state.write_update_state(finalize_after_restart(state, adapters.updater.installed(), now=1000.0))

    first_rollback = client.post("/api/v1/update/rollback", headers=CSRF_HEADERS)
    assert first_rollback.status_code == 202
    assert first_rollback.json()["job"]["target"] == "v1.0.0"

    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="abc")
    state = adapters.state.read_update_state()
    adapters.state.write_update_state(finalize_after_restart(state, adapters.updater.installed(), now=2000.0))
    status_after_first_rollback = client.get("/api/v1/update/status").json()
    assert status_after_first_rollback["previous_tag"] == "v2.0.0"

    second_rollback = client.post("/api/v1/update/rollback", headers=CSRF_HEADERS)

    assert second_rollback.status_code == 202
    body = second_rollback.json()
    assert body["job"]["target"] == "v2.0.0"
    assert adapters.updater.apply_calls == ["v2.0.0", "v1.0.0", "v2.0.0"]


def test_rollback_conflicts_with_an_in_progress_job(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.state.write_update_state(
        UpdateState(
            latest=None,
            checked_at=None,
            previous_tag="v1.0.0",
            job=UpdateJob(
                state="running",
                kind="update",
                target="v2.0.0",
                step="fetch",
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )

    response = client.post("/api/v1/update/rollback", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_in_progress"


# -- initial-mode session gate ------------------------------------------------------------------


def test_status_rejects_an_initial_mode_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.identity.identity = Identity(device_id="palmimo-042", initial_password_hash=hash_password("sticker"))
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "sticker"}, headers=CSRF_HEADERS)

    response = client.get("/api/v1/update/status")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "initial_password_must_be_changed"


# -- finalize_after_restart at startup (lifespan) ------------------------------------------------


def test_lifespan_finalizes_a_restarting_job_as_done_on_startup(tmp_path: Path) -> None:
    from palmimo_portal.api.app import create_app

    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=tmp_path / "static-not-built")
    app = create_app(settings)
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.updater.installed_version = InstalledVersion(tag="v2.0.0", commit="def")
    adapters.state.write_update_state(
        UpdateState(
            latest=RELEASE_V2,
            checked_at=1.0,
            previous_tag="v1.0.0",
            job=UpdateJob(
                state="restarting",
                kind="update",
                target="v2.0.0",
                step=None,
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )

    with TestClient(app):
        pass

    finalized = adapters.state.read_update_state()
    assert finalized.job.state == "done"


def test_lifespan_finalizes_a_restarting_job_as_failed_when_the_tag_does_not_match(tmp_path: Path) -> None:
    from palmimo_portal.api.app import create_app

    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=tmp_path / "static-not-built")
    app = create_app(settings)
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="stale")
    adapters.state.write_update_state(
        UpdateState(
            latest=RELEASE_V2,
            checked_at=1.0,
            previous_tag="v1.0.0",
            job=UpdateJob(
                state="restarting",
                kind="update",
                target="v2.0.0",
                step=None,
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )

    with TestClient(app):
        pass

    finalized = adapters.state.read_update_state()
    assert finalized.job.state == "failed"
    assert finalized.job.step == "restart"


# -- invalid release tags ------------------------------------------------------------------------


def test_check_rejects_an_invalid_tag_from_the_release_source(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.releases.latest = Release(
        tag="-v2.0.0", name="bad", published_at="2026-01-01T00:00:00Z", html_url="https://example.test/bad"
    )

    response = client.post("/api/v1/update/check", headers=CSRF_HEADERS)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "release_source_unavailable"
    # The invalid tag must not have been stored.
    assert adapters.state.read_update_state().latest is None


def test_apply_rejects_an_invalid_tag(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    _check_v2(client, adapters)

    response = client.post("/api/v1/update/apply", json={"tag": "-v2.0.0"}, headers=CSRF_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_release_tag"
    assert adapters.updater.apply_calls == []


# -- retry_available -----------------------------------------------------------------------------


def test_retry_available_is_true_after_a_failure_at_sync(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)
    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="abc")
    _check_v2(client, adapters)
    adapters.updater.fail_at_step = "sync"

    response = client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)

    assert response.json()["retry_available"] is True


def test_retry_available_is_false_when_idle(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _log_in(client, adapters)

    response = client.get("/api/v1/update/status")

    assert response.json()["retry_available"] is False


def test_apply_after_a_failed_job_can_retry_the_same_target(client: TestClient, adapters: FakeAdapterBundle) -> None:
    """The uv-sync-failed case: retry must work even though `update_available` would say no."""
    _log_in(client, adapters)
    adapters.updater.installed_version = InstalledVersion(tag="v1.0.0", commit="abc")
    _check_v2(client, adapters)
    adapters.updater.fail_at_step = "sync"
    first = client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)
    assert first.json()["job"]["state"] == "failed"

    adapters.updater.fail_at_step = None
    response = client.post("/api/v1/update/apply", json={"tag": "v2.0.0"}, headers=CSRF_HEADERS)

    assert response.status_code == 202
    assert response.json()["job"]["state"] == "restarting"
    assert adapters.updater.apply_calls == ["v2.0.0", "v2.0.0"]


def test_lifespan_leaves_an_idle_job_untouched(tmp_path: Path) -> None:
    from palmimo_portal.api.app import create_app

    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=tmp_path / "static-not-built")
    app = create_app(settings)
    adapters: FakeAdapterBundle = app.state.adapters

    with TestClient(app):
        pass

    assert adapters.state.read_update_state().job.state == "idle"


def test_lifespan_fails_a_running_job_left_over_from_before_this_boot(tmp_path: Path) -> None:
    """No thread can possibly be running a job at process start -- see finalize_after_restart's docstring."""
    from palmimo_portal.api.app import create_app

    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=tmp_path / "static-not-built")
    app = create_app(settings)
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.state.write_update_state(
        UpdateState(
            latest=RELEASE_V2,
            checked_at=1.0,
            previous_tag="v1.0.0",
            job=UpdateJob(
                state="running",
                kind="update",
                target="v2.0.0",
                step="sync",
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )

    with TestClient(app):
        pass

    finalized = adapters.state.read_update_state()
    assert finalized.job.state == "failed"
    assert finalized.job.step == "sync"
    assert "interrupted" in (finalized.job.error or "")
