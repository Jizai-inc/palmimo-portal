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
from palmimo_portal.core.apps_jobs import (
    AppsJobContext,
    PreparedInstall,
    disk_state_checks,
    install_git,
    prepare_install_zip,
)
from palmimo_portal.core.apps_start import AppRunningError
from palmimo_portal.ports import AppExistsError, AppsLockTimeoutError, AppsState, GitCommandError, UnitStatus
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


#: `_zip_bytes()`'s default manifest name is `zip`-namespaced (design doc 3.9);
#: the other constants mirror the other names installed by name below.
ZIP_ID = "zip.palmimo-teleop"
OTHER_APP_ID = "zip.other-app"
PALMIMO_OTHER_ID = "zip.palmimo-other"
#: `install_git`'s `url` below has no owner segment (`https://example.com/repo`),
#: so it fails normalization and falls back to the `git` namespace (design doc
#: 3.9's namespace-derivation table).
GIT_ID = "git.palmimo-teleop"


def _zip_bytes(name: str = "palmimo-teleop", requires_python: str | None = None) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("palmimo.toml", f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\n')
        requirement = f"requires-python = '{requires_python}'\n" if requires_python else ""
        archive.writestr("pyproject.toml", f"[project]\nname='app'\nversion='0'\n{requirement}")
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
    last_job = state_store.read_apps_state().apps[ZIP_ID].last_job
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
    assert state.current_job.display_name == "palmimo-teleop"
    assert state.current_job_app == ZIP_ID
    assert ZIP_ID not in state.apps  # not registered until commit_install finishes

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


def test_concurrent_install_starts_admit_exactly_one_job(ctx: AppsJobContext) -> None:
    """Two simultaneous starts must not both pass the pre-worker lock handoff."""
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=True)
    prepared = [prepare_install_zip(ctx, _zip_bytes(f"palmimo-teleop-{index}")) for index in range(2)]
    start_barrier = threading.Barrier(3)
    sync_started = threading.Event()
    release_sync = threading.Event()
    outcomes: list[str] = []

    def blocking_wait(instance: str, timeout_s: float) -> UnitStatus:
        sync_started.set()
        release_sync.wait(timeout=5)
        return UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0)

    def attempt(item: PreparedInstall) -> None:
        start_barrier.wait(timeout=5)
        try:
            runner.start_install(item)
            outcomes.append("accepted")
        except AppsLockTimeoutError:
            outcomes.append("locked")

    ctx.sync_unit.wait = blocking_wait  # type: ignore[method-assign]
    threads = [threading.Thread(target=attempt, args=(item,)) for item in prepared]
    for thread in threads:
        thread.start()
    start_barrier.wait(timeout=5)
    assert sync_started.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["accepted", "locked"]
    assert state_store.read_apps_state().current_job is not None
    release_sync.set()


def test_install_over_an_explicitly_requested_id_raises_before_spawning_a_thread(ctx: AppsJobContext) -> None:
    # An unspecified name auto-suffixes past a collision (design doc 3.9) -- only an
    # explicitly *requested* name component can still land on an already-occupied id.
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    runner.start_install(prepared)

    prepared_again = prepare_install_zip(ctx, _zip_bytes())
    with pytest.raises(AppExistsError):
        runner.start_install(prepared_again, requested_name="palmimo-teleop")


def test_start_install_keeps_the_failed_job_as_an_orphan_when_sync_fails(ctx: AppsJobContext) -> None:
    # A fresh install's name is never in `apps` when sync fails, so
    # `_fail_current_job` has no record to attach the failure to -- without the
    # orphan slot the failed job would be dropped from state on this same write.
    cast(FakeSyncUnitPort, ctx.sync_unit).default_status = UnitStatus(
        active_state="failed", sub_state="failed", result="exit-code", exec_main_status=1
    )
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())

    job = runner.start_install(prepared)

    assert job.state == "failed"
    state = state_store.read_apps_state()
    assert state.current_job is None
    assert ZIP_ID not in state.apps
    assert state.last_orphan_job is not None
    assert state.last_orphan_job.id == job.id
    assert state.last_orphan_job_app == ZIP_ID


def test_failed_install_discards_its_cache_before_the_id_is_reused(ctx: AppsJobContext) -> None:
    sync = cast(FakeSyncUnitPort, ctx.sync_unit)
    sync.default_status = UnitStatus(active_state="failed", sub_state="failed", result="exit-code", exec_main_status=1)
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())

    def leave_cache(instance: str, timeout_s: float) -> UnitStatus:
        (ctx.staging_dir / instance / ".uv-cache" / "untrusted").write_text("cached wheel")
        return sync.default_status

    sync.wait = leave_cache  # type: ignore[method-assign]
    assert runner.start_install(prepared).state == "failed"

    sync.default_status = UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0)
    seen: set[str] = set()

    def check_empty_cache(instance: str, timeout_s: float) -> UnitStatus:
        seen.update(path.name for path in (ctx.staging_dir / instance / ".uv-cache").iterdir())
        return sync.default_status

    sync.wait = check_empty_cache  # type: ignore[method-assign]
    assert runner.start_install(prepare_install_zip(ctx, _zip_bytes())).state == "done"
    assert seen == set()


def test_install_rejects_a_reused_id_when_its_stale_cache_cannot_be_purged(
    monkeypatch: pytest.MonkeyPatch, ctx: AppsJobContext
) -> None:
    from palmimo_portal.core import apps_jobs

    cache = ctx.uv_cache_dir / ZIP_ID
    cache.mkdir(parents=True)
    (cache / "untrusted").write_text("cached wheel")
    monkeypatch.setattr(apps_jobs, "purge_path", lambda _ctx, path: str(path))
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)

    job = runner.start_install(prepare_install_zip(ctx, _zip_bytes()))

    assert job.state == "failed"
    assert "stale uv cache" in (job.error or "")
    assert cast(FakeSyncUnitPort, ctx.sync_unit).start_calls == []


def test_start_install_reports_a_failed_required_python_install(ctx: AppsJobContext) -> None:
    uv = cast(FakeUvPort, ctx.uv)
    uv.system_python_satisfies = False
    uv.raise_on_install_python = RuntimeError("Python >=3.13 download failed")
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)

    job = runner.start_install(prepare_install_zip(ctx, _zip_bytes(requires_python=">=3.13")))

    assert job.state == "failed"
    assert "Python >=3.13 download failed" in (job.error or "")
    assert state_store.read_apps_state().last_orphan_job is not None


def test_a_later_successful_install_clears_a_previous_orphan_job(ctx: AppsJobContext) -> None:
    cast(FakeSyncUnitPort, ctx.sync_unit).default_status = UnitStatus(
        active_state="failed", sub_state="failed", result="exit-code", exec_main_status=1
    )
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    runner.start_install(prepare_install_zip(ctx, _zip_bytes("palmimo-teleop")))
    assert state_store.read_apps_state().last_orphan_job is not None

    cast(FakeSyncUnitPort, ctx.sync_unit).default_status = UnitStatus(
        active_state="inactive", sub_state="dead", result="success", exec_main_status=0
    )
    runner.start_install(prepare_install_zip(ctx, _zip_bytes("palmimo-other")))

    state = state_store.read_apps_state()
    assert state.last_orphan_job is None
    assert state.last_orphan_job_app is None
    assert PALMIMO_OTHER_ID in state.apps


def test_finalize_after_simulated_crash_mid_install_marks_interrupted_and_omits_the_app(ctx: AppsJobContext) -> None:
    # Simulate a process death right after `current_job`/`current_job_app` were persisted,
    # before commit_install ever ran -- the app must never appear half-installed.
    from palmimo_portal.ports import AppJob

    state_store = FakeStateStore()
    crashed_job = AppJob(
        id="crashed", kind="install", state="running", step="sync", error=None, started_at=1.0, finished_at=None
    )
    state_store.write_apps_state(AppsState(apps={}, current_job=crashed_job, current_job_app=ZIP_ID))

    manifest_exists, venv_exists = disk_state_checks(ctx, state_store.read_apps_state())
    finalized = finalize_apps_state(
        state_store.read_apps_state(), manifest_exists=manifest_exists, venv_exists=venv_exists, now=99.0
    )

    assert finalized.current_job is None
    assert ZIP_ID not in finalized.apps


def test_start_delete_refuses_while_the_app_unit_is_running(ctx: AppsJobContext) -> None:
    state_store = FakeStateStore()
    app_unit = FakeAppUnitPort()
    runner = AppsJobRunner(state_store, ctx, app_unit, run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    runner.start_install(prepared)
    app_unit.simulate_active_state(
        ZIP_ID, UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )

    with pytest.raises(AppRunningError):
        runner.start_delete(ZIP_ID)

    state = state_store.read_apps_state()
    assert ZIP_ID in state.apps
    assert (ctx.apps_dir / ZIP_ID).exists()


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
    other = state.apps[OTHER_APP_ID]
    state_store.write_apps_state(replace(state, apps={**state.apps, OTHER_APP_ID: replace(other, autostart=True)}))

    release_sync.set()
    for _ in range(200):
        if ZIP_ID in state_store.read_apps_state().apps:
            break
        time.sleep(0.01)

    final = state_store.read_apps_state()
    assert final.apps[OTHER_APP_ID].autostart is True
    assert ZIP_ID in final.apps


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
    runner.start_update(GIT_ID)
    assert sync_started.wait(timeout=5)

    state = state_store.read_apps_state()
    record = state.apps[GIT_ID]
    state_store.write_apps_state(replace(state, apps={**state.apps, GIT_ID: replace(record, autostart=True)}))

    release_sync.set()
    for _ in range(200):
        job = state_store.read_apps_state().apps[GIT_ID].last_job
        if job is not None and job.kind == "update" and job.state == "done":
            break
        time.sleep(0.01)

    final = state_store.read_apps_state().apps[GIT_ID]
    assert final.autostart is True
    assert final.source.commit == "commit-2"


def test_start_update_records_the_git_failure_reason_on_the_failed_job(ctx: AppsJobContext) -> None:
    # A 403 mid-update must land on `last_job.error_code`, not just its free-text `error` --
    # apart from "install_failed" (see `api/apps.py`'s `_raise_for_git_error` for the preview/
    # install-time equivalent this update path lacked).
    def seed(dest: Path, url: str, ref: str, ref_kind: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand=["run"]\n')
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    ctx.git.on_clone = seed  # type: ignore[attr-defined]
    ctx.git.next_commit = "commit-1"  # type: ignore[attr-defined]
    state_store = FakeStateStore()
    initial_state, _ = install_git(ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")
    state_store.write_apps_state(initial_state)
    ctx.git.raise_on_clone = GitCommandError(  # type: ignore[attr-defined]
        "403", status_code=403, reason="git_credential_rejected"
    )

    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    job = runner.start_update(GIT_ID)

    assert job.state == "failed"
    assert job.error_code == "git_credential_rejected"
    last_job = state_store.read_apps_state().apps[GIT_ID].last_job
    assert last_job is not None
    assert last_job.error_code == "git_credential_rejected"


def test_start_delete_failure_during_file_cleanup_restores_the_ledger_entry_as_failed(
    monkeypatch: pytest.MonkeyPatch, ctx: AppsJobContext
) -> None:
    state_store = FakeStateStore()
    runner = AppsJobRunner(state_store, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes())
    runner.start_install(prepared)
    ctx.secrets.set_secret("TOKEN", "s3cr3t")
    ctx.secrets.write_bindings(ZIP_ID, {"TOKEN": "TOKEN"})

    def raise_on_rename(self: Path, target: object) -> Path:
        raise OSError("device busy")

    monkeypatch.setattr(Path, "rename", raise_on_rename)

    job = runner.start_delete(ZIP_ID)

    assert job.state == "failed"
    record = state_store.read_apps_state().apps[ZIP_ID]
    assert record.last_job is not None
    assert record.last_job.state == "failed"
    assert ctx.secrets.read_bindings(ZIP_ID) == {"TOKEN": "TOKEN"}


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
    run_dir.write(ZIP_ID, env={"SECRET": "x"}, argv=["run"], cwd=str(ctx.apps_dir / ZIP_ID), project="p")

    runner.start_delete(ZIP_ID)

    assert ZIP_ID not in run_dir.written


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

    job = runner.start_delete(ZIP_ID)

    assert job.state == "done", "the ledger removal already succeeded -- a leftover is a warning, not a job failure"
    assert job.error is not None
    assert "stuck-trash" in job.error
    assert ZIP_ID not in state_store.read_apps_state().apps

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
