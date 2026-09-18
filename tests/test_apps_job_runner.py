"""Behavioral tests for AppsJobRunner: background execution, visibility while running, and crash recovery."""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from palmimo_portal.core.apps import finalize_apps_state
from palmimo_portal.core.apps_job_runner import AppsJobRunner
from palmimo_portal.core.apps_jobs import AppsJobContext, disk_state_checks, install_git, prepare_install_zip
from palmimo_portal.core.apps_start import AppRunningError
from palmimo_portal.ports import AppExistsError, AppsLockTimeoutError, AppsState, UnitStatus
from palmimo_portal.testing.fakes import (
    FakeAppUnitPort,
    FakeDiskPort,
    FakeGitPort,
    FakeJournalPort,
    FakeRunDirPort,
    FakeSecretsStore,
    FakeStateStore,
    FakeSyncUnitPort,
    FakeUvPort,
)


def _zip_bytes(name: str = "palmimo-teleop") -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("palmimo.toml", f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\n')
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\n")
    return buffer.getvalue()


@pytest.fixture
def ctx(tmp_path: Path) -> AppsJobContext:
    return AppsJobContext(
        apps_dir=tmp_path / "apps",
        uv_cache_dir=tmp_path / "uv-cache",
        git=FakeGitPort(),
        uv=FakeUvPort(),
        sync_unit=FakeSyncUnitPort(staging_dir=tmp_path / "apps" / ".staging"),
        journal=FakeJournalPort(),
        disk=FakeDiskPort(),
        secrets=FakeSecretsStore(),
    )


def test_start_install_completes_and_registers_the_app_when_run_inline(ctx: AppsJobContext) -> None:
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())

    job = runner.start_install(prepared)

    assert job.state == "done"
    last_job = state_store.read_apps_state().apps["palmimo-teleop"].last_job
    assert last_job is not None
    assert last_job.id == job.id
    assert state_store.read_apps_state().current_job is None


def test_install_job_is_visible_as_running_while_sync_is_in_flight(ctx: AppsJobContext) -> None:
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=True)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    sync_started = threading.Event()
    release_sync = threading.Event()

    def blocking_wait(instance: str, timeout_s: float) -> UnitStatus:
        sync_started.set()
        release_sync.wait(timeout=5)
        return UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0)

    ctx.sync_unit.wait = blocking_wait  # type: ignore[method-assign]

    job = runner.start_install(prepared)
    assert job.state == "running"
    assert sync_started.wait(timeout=5)

    state = state_store.read_apps_state()
    assert state.current_job is not None
    assert state.current_job.id == job.id
    assert state.current_job.state == "running"
    assert state.current_job_app == "palmimo-teleop"
    assert "palmimo-teleop" not in state.apps  # not registered until commit_install finishes

    release_sync.set()


def test_second_install_while_a_job_is_running_returns_lock_timeout(ctx: AppsJobContext) -> None:
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=True)
    prepared_a = prepare_install_zip(ctx, _zip_bytes("palmimo-teleop-a"))
    sync_started = threading.Event()
    release_sync = threading.Event()

    def blocking_wait(instance: str, timeout_s: float) -> UnitStatus:
        sync_started.set()
        release_sync.wait(timeout=5)
        return UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0)

    ctx.sync_unit.wait = blocking_wait  # type: ignore[method-assign]
    runner.start_install(prepared_a)
    assert sync_started.wait(timeout=5)

    prepared_b = prepare_install_zip(ctx, _zip_bytes("palmimo-teleop-b"))
    with pytest.raises(AppsLockTimeoutError):
        runner.start_install(prepared_b)

    release_sync.set()


def test_install_over_existing_name_raises_before_spawning_a_thread(ctx: AppsJobContext) -> None:
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    runner.start_install(prepared)

    prepared_again = prepare_install_zip(ctx, _zip_bytes())
    with pytest.raises(AppExistsError):
        runner.start_install(prepared_again)


def test_finalize_after_simulated_crash_mid_install_marks_interrupted_and_omits_the_app(ctx: AppsJobContext) -> None:
    # Simulate a process death right after `current_job`/`current_job_app` were persisted,
    # before commit_install ever ran -- the app must never appear half-installed.
    from palmimo_portal.ports import AppJob

    state_store = FakeStateStore()
    crashed_job = AppJob(
        id="crashed", kind="install", state="running", step="sync", error=None, started_at=1.0, finished_at=None
    )
    state_store.write_apps_state(AppsState(apps={}, current_job=crashed_job, current_job_app="palmimo-teleop"))

    manifest_exists, venv_exists = disk_state_checks(ctx, state_store.read_apps_state())
    finalized = finalize_apps_state(
        state_store.read_apps_state(), manifest_exists=manifest_exists, venv_exists=venv_exists, now=99.0
    )

    assert finalized.current_job is None
    assert "palmimo-teleop" not in finalized.apps


def test_start_delete_refuses_while_the_app_unit_is_running(ctx: AppsJobContext) -> None:
    state_store = FakeStateStore()
    app_unit = FakeAppUnitPort()
    runner = AppsJobRunner(state_store, ctx, app_unit, run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    runner.start_install(prepared)
    app_unit.simulate_active_state(
        "palmimo-teleop", UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )

    with pytest.raises(AppRunningError):
        runner.start_delete("palmimo-teleop")

    state = state_store.read_apps_state()
    assert "palmimo-teleop" in state.apps
    assert (ctx.apps_dir / "palmimo-teleop").exists()


def test_job_completion_does_not_clobber_a_concurrent_write_to_another_apps_record(ctx: AppsJobContext) -> None:
    """The install job's own read of `apps.json` happens before `uv sync` starts running --
    a concurrent write to an unrelated app (e.g. `PUT .../autostart`) landing while sync is
    in flight must survive the job's own write-back at completion."""
    state_store = FakeStateStore()
    other_prepared = prepare_install_zip(ctx, _zip_bytes("other-app"))
    AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False).start_install(other_prepared)

    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=True)
    prepared = prepare_install_zip(ctx, _zip_bytes("palmimo-teleop"))
    sync_started = threading.Event()
    release_sync = threading.Event()

    def blocking_wait(instance: str, timeout_s: float) -> UnitStatus:
        sync_started.set()
        release_sync.wait(timeout=5)
        return UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0)

    ctx.sync_unit.wait = blocking_wait  # type: ignore[method-assign]
    runner.start_install(prepared)
    assert sync_started.wait(timeout=5)

    state = state_store.read_apps_state()
    other = state.apps["other-app"]
    state_store.write_apps_state(replace(state, apps={**state.apps, "other-app": replace(other, autostart=True)}))

    release_sync.set()
    for _ in range(200):
        if "palmimo-teleop" in state_store.read_apps_state().apps:
            break
        time.sleep(0.01)

    final = state_store.read_apps_state()
    assert final.apps["other-app"].autostart is True
    assert "palmimo-teleop" in final.apps


def test_update_completion_keeps_a_concurrent_autostart_change_to_the_same_app(ctx: AppsJobContext) -> None:
    """The update job reads `apps.json` at the start of its fetch/sync -- a `PUT .../autostart`
    landing on the *same* app while sync is in flight must survive the job's own write-back,
    which must still apply the ref/commit/manifest the job itself fetched."""

    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand=["run"]\n')
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    ctx.git.on_clone = seed  # type: ignore[attr-defined]
    ctx.git.next_commit = "commit-1"  # type: ignore[attr-defined]
    state_store = FakeStateStore()
    initial_state, _ = install_git(ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")
    state_store.write_apps_state(initial_state)

    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=True)
    sync_started = threading.Event()
    release_sync = threading.Event()

    def blocking_wait(instance: str, timeout_s: float) -> UnitStatus:
        sync_started.set()
        release_sync.wait(timeout=5)
        return UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0)

    ctx.sync_unit.wait = blocking_wait  # type: ignore[method-assign]
    ctx.git.next_commit = "commit-2"  # type: ignore[attr-defined]
    runner.start_update("palmimo-teleop")
    assert sync_started.wait(timeout=5)

    state = state_store.read_apps_state()
    record = state.apps["palmimo-teleop"]
    state_store.write_apps_state(replace(state, apps={**state.apps, "palmimo-teleop": replace(record, autostart=True)}))

    release_sync.set()
    for _ in range(200):
        job = state_store.read_apps_state().apps["palmimo-teleop"].last_job
        if job is not None and job.kind == "update" and job.state == "done":
            break
        time.sleep(0.01)

    final = state_store.read_apps_state().apps["palmimo-teleop"]
    assert final.autostart is True
    assert final.source.commit == "commit-2"


def test_start_delete_failure_during_file_cleanup_restores_the_ledger_entry_as_failed(
    monkeypatch: pytest.MonkeyPatch, ctx: AppsJobContext
) -> None:
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    runner.start_install(prepared)
    ctx.secrets.set_secret("TOKEN", "s3cr3t")
    ctx.secrets.write_bindings("palmimo-teleop", {"TOKEN": "TOKEN"})

    def raise_on_rename(self: Path, target: object) -> Path:
        raise OSError("device busy")

    monkeypatch.setattr(Path, "rename", raise_on_rename)

    job = runner.start_delete("palmimo-teleop")

    assert job.state == "failed"
    record = state_store.read_apps_state().apps["palmimo-teleop"]
    assert record.last_job is not None
    assert record.last_job.state == "failed"
    assert ctx.secrets.read_bindings("palmimo-teleop") == {"TOKEN": "TOKEN"}


def test_start_delete_removes_the_apps_run_directory(ctx: AppsJobContext) -> None:
    # A run dir left by a failed start carries a secrets-bearing env file -- delete must not
    # leave it behind once the app itself is gone.
    from dataclasses import replace

    state_store = FakeStateStore()
    run_dir = FakeRunDirPort()
    ctx = replace(ctx, run_dir=run_dir)
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    runner.start_install(prepared)
    run_dir.write(
        "palmimo-teleop", env={"SECRET": "x"}, argv=["run"], cwd=str(ctx.apps_dir / "palmimo-teleop"), project="p"
    )

    runner.start_delete("palmimo-teleop")

    assert "palmimo-teleop" not in run_dir.written


def test_start_delete_reports_a_removal_failure_that_purge_mode_also_cannot_fix(
    monkeypatch: pytest.MonkeyPatch, ctx: AppsJobContext
) -> None:
    from palmimo_portal.core import apps_jobs

    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    runner.start_install(prepared)

    trash_container = ctx.trash_dir / "stuck-trash"
    real_rmtree = shutil.rmtree

    def fail_only_the_trash_container(path: str | os.PathLike[str], ignore_errors: bool = False) -> None:
        if Path(path) == trash_container:
            raise OSError("busy")
        real_rmtree(path, ignore_errors=ignore_errors)

    ctx.new_id = lambda: "stuck-trash"
    monkeypatch.setattr(apps_jobs.shutil, "rmtree", fail_only_the_trash_container)
    sync_unit = cast(FakeSyncUnitPort, ctx.sync_unit)
    sync_unit.undeletable_paths.add(str(trash_container))

    job = runner.start_delete("palmimo-teleop")

    assert job.state == "done", "the ledger removal already succeeded -- a leftover is a warning, not a job failure"
    assert job.error is not None
    assert "stuck-trash" in job.error
    assert "palmimo-teleop" not in state_store.read_apps_state().apps

    purge_instance = sync_unit.start_calls[-1]
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,39}", purge_instance), purge_instance
    assert sync_unit.sync_specs[purge_instance] == {"purge": str(trash_container)}
    assert not (ctx.staging_dir / purge_instance).exists()


def test_start_install_releases_the_lock_when_the_worker_thread_fails_to_start(
    monkeypatch: pytest.MonkeyPatch, ctx: AppsJobContext
) -> None:
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=True)
    prepared = prepare_install_zip(ctx, _zip_bytes())

    def raise_on_start(self: threading.Thread) -> None:
        raise RuntimeError("cannot start a thread")

    monkeypatch.setattr(threading.Thread, "start", raise_on_start)

    with pytest.raises(RuntimeError):
        runner.start_install(prepared)

    with state_store.lock_apps():
        pass  # the lock must be free again despite the failed spawn
