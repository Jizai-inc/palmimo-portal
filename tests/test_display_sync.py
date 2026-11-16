import logging
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI

from palmimo_portal.api.app import create_app
from palmimo_portal.core.apps_start import stop_app
from palmimo_portal.core.display_sync import DisplaySyncService
from palmimo_portal.core.periodic import PeriodicScheduler, PeriodicTask
from palmimo_portal.ports import (
    AppRecord,
    AppSource,
    AppsState,
    DisplaySyncPending,
    DisplaySyncResult,
    DisplayToolError,
    PlatformCommandError,
    PlatformVerifyDiff,
)
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle, FakeClockPort, FakePlatformPort, FakeStateStore


@pytest.mark.parametrize("outcome", ["synced", "busy", "skipped", "failed"])
def test_retry_pending_preserves_unsynced_work(outcome: str) -> None:
    state = FakeStateStore()
    pending = DisplaySyncPending(True, "old", "old-boot")
    state.write_display_sync(pending)
    platform = FakePlatformPort(
        boot_id="boot", display_sync_result=DisplaySyncResult.from_payload({"result": outcome, "reason": "new"})
    )
    service = DisplaySyncService(state, platform)
    service.retry_pending()
    assert platform.sync_display_assets_calls == [None]
    assert state.read_display_sync() == (
        None if outcome == "synced" else DisplaySyncPending(True, "new", "boot") if outcome == "busy" else pending
    )
    assert not service.running


def test_retry_pending_skips_when_run_lock_is_held() -> None:
    state = FakeStateStore()
    state.write_display_sync(DisplaySyncPending(True, None, None))
    platform = FakePlatformPort()
    service = DisplaySyncService(state, platform)
    with state.lock_run():
        service.retry_pending()
    assert platform.sync_display_assets_calls == []
    service.retry_pending()
    assert platform.sync_display_assets_calls == [None]


@pytest.mark.parametrize("fail", [False, True])
def test_sync_running_clears_after_tool_returns_or_raises(fail: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    state = FakeStateStore()
    platform = FakePlatformPort()
    service = DisplaySyncService(state, platform)
    observed: list[bool] = []

    def tool(app_id: str | None) -> DisplaySyncResult:
        observed.append(service.running)
        if fail:
            raise RuntimeError("tool unavailable")
        return DisplaySyncResult("synced", (), (), None)

    monkeypatch.setattr(platform, "sync_display_assets", tool)
    if fail:
        with pytest.raises(RuntimeError):
            service.sync()
    else:
        assert service.sync().result == "synced"
    assert observed == [True]
    assert not service.running
    with state.lock_run():
        pass


@pytest.mark.parametrize("outcome", ["busy", "synced"])
def test_sync_record_failure_preserves_tool_result(outcome: str, monkeypatch: pytest.MonkeyPatch) -> None:
    state = FakeStateStore()
    platform = FakePlatformPort(display_sync_result=DisplaySyncResult.from_payload({"result": outcome}))
    service = DisplaySyncService(state, platform)

    def fail(pending: DisplaySyncPending | None) -> None:
        raise OSError("full")

    monkeypatch.setattr(state, "write_display_sync", fail)
    assert service.sync().result == outcome
    assert not service.running


def test_stop_schedules_pending_sync_without_waiting(tmp_path: Path) -> None:
    app = create_app(Settings(apps_dir=tmp_path, periodic_enabled=False, autostart_enabled=False))
    app.state.platform_verify_diffs = []
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.state.write_apps_state(AppsState(apps={"face": AppRecord("face", AppSource("zip"), 0, {}, False, None)}))
    adapters.state.write_display_sync(DisplaySyncPending(True, None, None))
    stop_app(app.state.start_deps, "face")
    assert adapters.platform.sync_display_assets_calls == []
    app.state.periodic_scheduler.tick()
    assert adapters.platform.sync_display_assets_calls == [None]


def test_scheduler_runs_local_sync_without_ntp() -> None:
    calls: list[str] = []
    clock = FakeClockPort(synchronized=False)
    scheduler = PeriodicScheduler(
        clock,
        [
            PeriodicTask("network", 60, lambda: calls.append("network")),
            PeriodicTask("display_sync", 60, lambda: calls.append("sync"), requires_ntp=False),
        ],
    )
    clock.monotonic_value += 60
    scheduler.tick()
    assert calls == ["sync"]
    clock.synchronized = True
    scheduler.tick()
    assert calls == ["sync", "network"]


def test_scheduler_trigger_during_run_is_coalesced_without_overlap() -> None:
    clock = FakeClockPort()
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def work() -> None:
        calls.append(1)
        entered.set()
        assert release.wait(3)

    scheduler = PeriodicScheduler(clock, [PeriodicTask("sync", 60, work)])
    scheduler.trigger("sync")
    worker = threading.Thread(target=scheduler.tick)
    worker.start()
    try:
        assert entered.wait(3)
        scheduler.trigger("sync")
        scheduler.trigger("sync")
        scheduler.tick()
        assert calls == [1]
    finally:
        release.set()
        worker.join(3)
    scheduler.tick()
    scheduler.tick()
    assert calls == [1, 1]


def test_retry_pending_does_not_sync_without_pending_work() -> None:
    state = FakeStateStore()
    platform = FakePlatformPort()
    DisplaySyncService(state, platform).retry_pending()
    assert platform.sync_display_assets_calls == []


@pytest.mark.parametrize(
    "failure",
    [
        DisplaySyncResult("failed", (), ("palmimo/system.idle",), "transfer failed"),
        PlatformCommandError("display_assets", "timeout"),
        DisplayToolError("invalid output"),
    ],
)
def test_destructive_sync_failure_is_retried_by_periodic_task(
    tmp_path: Path, failure: DisplaySyncResult | Exception
) -> None:
    app = create_app(Settings(apps_dir=tmp_path, periodic_enabled=False, autostart_enabled=False))
    app.state.platform_verify_diffs = []
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.platform.boot_id = "boot"
    reason: str | None
    if isinstance(failure, Exception):
        adapters.platform.display_error = failure
        with pytest.raises(type(failure)):
            app.state.display_sync.sync("zip.face")
        reason = str(failure)
    else:
        adapters.platform.display_sync_result = failure
        assert app.state.display_sync.sync("zip.face") == failure
        reason = failure.reason

    assert adapters.state.read_display_sync() == DisplaySyncPending(True, reason, "boot")
    assert not app.state.display_sync.running
    adapters.platform.display_error = None
    adapters.platform.display_sync_result = DisplaySyncResult("synced", ("palmimo/system.idle",), (), None)
    adapters.clock.monotonic_value += 60
    app.state.periodic_scheduler.tick()
    assert adapters.platform.sync_display_assets_calls == ["zip.face", None]
    assert adapters.state.read_display_sync() is None


@pytest.mark.parametrize(
    "failure",
    [
        PlatformCommandError("display_assets", "unavailable"),
        DisplayToolError("unavailable"),
        DisplaySyncResult("failed", (), (), "unavailable"),
    ],
)
def test_periodic_sync_backs_off_repeated_failures_without_tracebacks(
    failure: Exception | DisplaySyncResult, caplog: pytest.LogCaptureFixture
) -> None:
    state = FakeStateStore()
    pending = DisplaySyncPending(True, "pending", "boot")
    state.write_display_sync(pending)
    platform = FakePlatformPort()
    if isinstance(failure, Exception):
        platform.display_error = failure
    else:
        platform.display_sync_result = failure
    clock = FakeClockPort()
    service = DisplaySyncService(state, platform, monotonic=clock.monotonic)
    scheduler = PeriodicScheduler(clock, [PeriodicTask("display_sync", 60, service.retry_pending, requires_ntp=False)])
    for _ in range(6):
        clock.monotonic_value += 60
        scheduler.tick()
    assert 1 < len(platform.sync_display_assets_calls) < 6
    records = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING and records[0].exc_info is None
    assert state.read_display_sync() is not None
    assert not service.running
    platform.display_error = None
    platform.display_sync_result = DisplaySyncResult("synced", (), (), None)
    clock.monotonic_value += 900
    scheduler.tick()
    assert state.read_display_sync() is None
    assert len(platform.sync_display_assets_calls) < 7
    caplog.clear()
    state.write_display_sync(pending)
    if isinstance(failure, Exception):
        platform.display_error = failure
    else:
        platform.display_sync_result = failure
    clock.monotonic_value += 60
    scheduler.tick()
    assert state.read_display_sync() is not None
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("unready", ["missing", "outdated", "unverified", "drift"])
def test_periodic_sync_waits_for_platform_readiness(app: FastAPI, unready: str) -> None:
    adapters: FakeAdapterBundle = app.state.adapters
    pending = DisplaySyncPending(True, "pending", "boot")
    adapters.state.write_display_sync(pending)
    installed = adapters.platform.installed
    assert installed is not None
    app.state.platform_verify_diffs = None if unready == "unverified" else []
    if unready == "missing":
        adapters.platform.installed = None
    elif unready == "outdated":
        adapters.platform.installed = replace(installed, version=app.state.settings.required_platform_version - 1)
    elif unready == "drift":
        app.state.platform_verify_diffs = [PlatformVerifyDiff("/changed", "changed")]
    for _ in range(3):
        adapters.clock.monotonic_value += 60
        app.state.periodic_scheduler.tick()
    assert adapters.platform.sync_display_assets_calls == []
    assert adapters.state.read_display_sync() == pending
    adapters.platform.installed = installed
    app.state.platform_verify_diffs = []
    adapters.clock.monotonic_value += 60
    app.state.periodic_scheduler.tick()
    assert adapters.platform.sync_display_assets_calls == [None]
    assert adapters.state.read_display_sync() is None
