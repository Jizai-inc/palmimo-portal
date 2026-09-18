"""Behavioral tests for the periodic scheduler and its git-check sweep (design doc 3.2/3.6)."""

from __future__ import annotations

import io
import threading
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from palmimo_portal.core.apps_job_runner import AppsJobRunner
from palmimo_portal.core.apps_jobs import AppsJobContext, prepare_install_zip
from palmimo_portal.core.periodic import PeriodicScheduler, PeriodicTask, run_git_check_sweep
from palmimo_portal.ports import AppRecord, AppRefKind, AppSource, AppsState, GitCommandError
from palmimo_portal.testing.fakes import (
    FakeAppUnitPort,
    FakeClockPort,
    FakeDiskPort,
    FakeGitPort,
    FakeJournalPort,
    FakeSecretsStore,
    FakeStateStore,
    FakeSyncUnitPort,
    FakeUvPort,
)


def _zip_bytes(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("palmimo.toml", f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\n')
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\n")
    return buffer.getvalue()


def _ctx(tmp_path: Path, git: FakeGitPort, secrets: FakeSecretsStore | None = None) -> AppsJobContext:
    return AppsJobContext(
        apps_dir=tmp_path / "apps",
        uv_cache_dir=tmp_path / "uv-cache",
        git=git,
        uv=FakeUvPort(),
        sync_unit=FakeSyncUnitPort(),
        journal=FakeJournalPort(),
        disk=FakeDiskPort(),
        secrets=secrets or FakeSecretsStore(),
    )


def _git_record(name: str, *, url: str, commit: str | None, credential_rejected: bool = False) -> AppRecord:
    return AppRecord(
        name=name,
        source=AppSource(type="git", url=url, ref="main", ref_kind="branch", commit=commit),
        installed_at=1.0,
        params={},
        autostart=False,
        last_job=None,
        credential_rejected=credential_rejected,
    )


def test_scheduler_fires_task_only_after_its_interval_elapses_on_monotonic_time() -> None:
    clock = FakeClockPort(monotonic_value=0.0)
    calls: list[int] = []
    scheduler = PeriodicScheduler(clock, [PeriodicTask("t", 100.0, lambda: calls.append(1))])

    scheduler.tick()
    assert calls == []

    clock.monotonic_value = 99.0
    scheduler.tick()
    assert calls == [], "must not fire before its interval has elapsed"

    clock.monotonic_value = 100.0
    scheduler.tick()
    assert calls == [1]


def test_scheduler_does_not_fire_a_task_twice_within_one_interval() -> None:
    clock = FakeClockPort(monotonic_value=0.0)
    calls: list[int] = []
    scheduler = PeriodicScheduler(clock, [PeriodicTask("t", 100.0, lambda: calls.append(1))])
    clock.monotonic_value = 100.0

    scheduler.tick()
    scheduler.tick()

    assert calls == [1]


def test_scheduler_skips_every_task_and_logs_once_per_streak_when_clock_unsynchronized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClockPort(monotonic_value=0.0, synchronized=False)
    calls: list[int] = []
    scheduler = PeriodicScheduler(clock, [PeriodicTask("t", 100.0, lambda: calls.append(1))])
    clock.monotonic_value = 100.0

    with caplog.at_level("INFO"):
        scheduler.tick()
        scheduler.tick()
        scheduler.tick()

    assert calls == [], "a network task must never run before the clock is NTP-synchronized"
    skip_logs = [r for r in caplog.records if "clock_unsynced" in r.message]
    assert len(skip_logs) == 1, "the skip must be logged once per streak, not on every tick"


def test_scheduler_resumes_once_the_clock_synchronizes() -> None:
    clock = FakeClockPort(monotonic_value=0.0, synchronized=False)
    calls: list[int] = []
    scheduler = PeriodicScheduler(clock, [PeriodicTask("t", 100.0, lambda: calls.append(1))])
    clock.monotonic_value = 100.0
    scheduler.tick()
    assert calls == []

    clock.synchronized = True
    scheduler.tick()

    assert calls == [1]


def test_git_check_sweep_marks_update_available_when_a_newer_commit_exists(tmp_path: Path) -> None:
    git = FakeGitPort(remote_commits={("https://example.com/repo", "main", "branch"): "new-commit"})
    ctx = _ctx(tmp_path, git)
    state = FakeStateStore()
    state.write_apps_state(
        AppsState(apps={"app-a": _git_record("app-a", url="https://example.com/repo", commit="old-commit")})
    )

    run_git_check_sweep(ctx, state)

    record = state.read_apps_state().apps["app-a"]
    assert record.update_available is True
    assert record.latest_commit == "new-commit"


def test_git_check_sweep_clears_update_available_once_the_ledger_commit_catches_up(tmp_path: Path) -> None:
    git = FakeGitPort(remote_commits={("https://example.com/repo", "main", "branch"): "same-commit"})
    ctx = _ctx(tmp_path, git)
    state = FakeStateStore()
    record = _git_record("app-a", url="https://example.com/repo", commit="same-commit")
    record = replace(record, update_available=True, latest_commit="same-commit")
    state.write_apps_state(AppsState(apps={"app-a": record}))

    run_git_check_sweep(ctx, state)

    assert state.read_apps_state().apps["app-a"].update_available is False


def test_git_check_sweep_marks_credential_rejected_on_401_and_skips_it_next_tick(tmp_path: Path) -> None:
    key = ("https://github.com/acme/repo-a", "main", "branch")
    git = FakeGitPort(raise_on_fetch_commit_for={key: GitCommandError("nope", status_code=401)})
    secrets = FakeSecretsStore()
    secrets.set_git_credential("github.com/acme", "token")
    ctx = _ctx(tmp_path, git, secrets)
    state = FakeStateStore()
    state.write_apps_state(
        AppsState(apps={"app-a": _git_record("app-a", url="https://github.com/acme/repo-a", commit="c1")})
    )

    run_git_check_sweep(ctx, state)
    assert state.read_apps_state().apps["app-a"].credential_rejected is True

    run_git_check_sweep(ctx, state)

    assert len(git.fetch_commit_calls) == 1, "a rejected credential must not be retried on the next sweep"


def test_git_check_sweep_does_not_mark_credential_rejected_when_no_credential_is_registered(tmp_path: Path) -> None:
    key = ("https://github.com/acme/repo-a", "main", "branch")
    git = FakeGitPort(raise_on_fetch_commit_for={key: GitCommandError("nope", status_code=401)})
    ctx = _ctx(tmp_path, git, FakeSecretsStore())
    state = FakeStateStore()
    state.write_apps_state(
        AppsState(apps={"app-a": _git_record("app-a", url="https://github.com/acme/repo-a", commit="c1")})
    )

    run_git_check_sweep(ctx, state)

    assert state.read_apps_state().apps["app-a"].credential_rejected is False


def test_git_check_sweep_marks_every_app_sharing_the_rejected_credential(tmp_path: Path) -> None:
    key = ("https://github.com/acme/repo-a", "main", "branch")
    git = FakeGitPort(raise_on_fetch_commit_for={key: GitCommandError("nope", status_code=403)})
    secrets = FakeSecretsStore()
    secrets.set_git_credential("github.com/acme", "token")
    ctx = _ctx(tmp_path, git, secrets)
    state = FakeStateStore()
    state.write_apps_state(
        AppsState(
            apps={
                "app-a": _git_record("app-a", url="https://github.com/acme/repo-a", commit="c1"),
                "app-b": _git_record("app-b", url="https://github.com/acme/repo-b", commit="c1"),
                "app-c": _git_record("app-c", url="https://github.com/other/repo-c", commit="c1"),
            }
        )
    )

    run_git_check_sweep(ctx, state)

    apps = state.read_apps_state().apps
    assert apps["app-a"].credential_rejected is True
    assert apps["app-b"].credential_rejected is True, "same host/owner credential must also be marked"
    assert apps["app-c"].credential_rejected is False, "an unrelated credential must not be marked"


def test_git_check_sweep_does_not_hold_apps_lock_during_its_network_calls(tmp_path: Path) -> None:
    git = FakeGitPort(remote_commits={("https://example.com/repo", "main", "branch"): "new-commit"})
    ctx = _ctx(tmp_path, git)
    state = FakeStateStore()
    state.write_apps_state(
        AppsState(apps={"app-a": _git_record("app-a", url="https://example.com/repo", commit="old-commit")})
    )
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    original_fetch_commit = git.fetch_commit

    def blocking_fetch_commit(url: str, ref: str, ref_kind: AppRefKind, *, env: Mapping[str, str] | None = None) -> str:
        fetch_started.set()
        release_fetch.wait(timeout=5)
        return original_fetch_commit(url, ref, ref_kind, env=env)

    git.fetch_commit = blocking_fetch_commit  # type: ignore[method-assign]

    sweep_thread = threading.Thread(target=run_git_check_sweep, args=(ctx, state), daemon=True)
    sweep_thread.start()
    assert fetch_started.wait(timeout=5)

    runner = AppsJobRunner(state, ctx, FakeAppUnitPort(), run_in_thread=False)
    prepared = prepare_install_zip(ctx, _zip_bytes("other-app"))
    job = runner.start_install(prepared)
    assert job.state == "done", "an app job must be able to acquire apps.lock while the sweep's network call blocks"

    release_fetch.set()
    sweep_thread.join(timeout=5)


def test_git_check_sweep_write_back_preserves_a_concurrent_autostart_change(tmp_path: Path) -> None:
    git = FakeGitPort(remote_commits={("https://example.com/repo", "main", "branch"): "new-commit"})
    ctx = _ctx(tmp_path, git)
    state = FakeStateStore()
    state.write_apps_state(
        AppsState(apps={"app-a": _git_record("app-a", url="https://example.com/repo", commit="old-commit")})
    )
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    original_fetch_commit = git.fetch_commit

    def blocking_fetch_commit(url: str, ref: str, ref_kind: AppRefKind, *, env: Mapping[str, str] | None = None) -> str:
        fetch_started.set()
        release_fetch.wait(timeout=5)
        return original_fetch_commit(url, ref, ref_kind, env=env)

    git.fetch_commit = blocking_fetch_commit  # type: ignore[method-assign]

    sweep_thread = threading.Thread(target=run_git_check_sweep, args=(ctx, state), daemon=True)
    sweep_thread.start()
    assert fetch_started.wait(timeout=5)

    # Simulates a `PUT .../autostart` landing on the same app while the sweep's
    # `git ls-remote` is still in flight.
    current = state.read_apps_state()
    state.write_apps_state(AppsState(apps={**current.apps, "app-a": replace(current.apps["app-a"], autostart=True)}))

    release_fetch.set()
    sweep_thread.join(timeout=5)

    record = state.read_apps_state().apps["app-a"]
    assert record.autostart is True, "the sweep's write-back must not revert a concurrent PUT"
    assert record.update_available is True, "the sweep's own result must still land"


def test_git_check_sweep_is_skipped_while_an_apps_job_holds_the_lock(tmp_path: Path) -> None:
    git = FakeGitPort(remote_commits={("https://example.com/repo", "main", "branch"): "new-commit"})
    ctx = _ctx(tmp_path, git)
    state = FakeStateStore()
    state.write_apps_state(
        AppsState(apps={"app-a": _git_record("app-a", url="https://example.com/repo", commit="old-commit")})
    )

    with state.lock_apps():
        run_git_check_sweep(ctx, state)

    assert git.fetch_commit_calls == []
    assert state.read_apps_state().apps["app-a"].update_available is False
