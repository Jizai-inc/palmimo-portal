import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from palmimo_portal.api.app import create_app
from palmimo_portal.core.apps_start import AppStartError, StartDeps, start_app
from palmimo_portal.core.manifest import manifest_snapshot, parse_manifest
from palmimo_portal.ports import (
    AppRecord,
    AppSource,
    AppsState,
    DisplayDesireResult,
    DisplayFirmwareNeed,
    DisplayFirmwareResult,
    DisplayProtocol,
    DisplaySyncPending,
    DisplaySyncResult,
)
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


APP_ID = "test.face"


@pytest.fixture
def env(tmp_path: Path) -> tuple[StartDeps, FakeAdapterBundle]:
    app = create_app(Settings(apps_dir=tmp_path / "apps", periodic_enabled=False, autostart_enabled=False))
    adapters: FakeAdapterBundle = app.state.adapters
    project = tmp_path / "apps" / APP_ID
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    manifest = parse_manifest(
        'schema=2\nname="face"\ndescription="d"\ncommand=["run"]\ndevices=["motor_display"]\n[assets.welcome]\nfile="face.json"\ndescription="Welcome"\ntags=["greeting"]\n[assets.welcome.params.eye]\ndescription="Eye"'
    )
    (project / "palmimo.toml.i18n.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "translations": {
                    "ja": {"assets.welcome": "Translated welcome", "assets.welcome.params.eye": "Translated eye"}
                },
            }
        )
    )
    record = AppRecord("face", AppSource("zip"), 0, {}, False, None, id=APP_ID, manifest=manifest_snapshot(manifest))
    adapters.state.write_apps_state(AppsState(apps={APP_ID: record}))
    adapters.platform.boot_id = "boot-a"
    adapters.app_environment.display_firmware_need = "2"
    return replace(app.state.start_deps, platform_ready=lambda: True), adapters


@pytest.mark.parametrize(
    ("need", "device", "firmware", "sync"),
    [("none", True, 0, []), ("1", True, 1, []), ("2", True, 1, [APP_ID]), ("2", False, 0, [])],
)
def test_start_runs_only_required_display_stages(
    env: tuple[StartDeps, FakeAdapterBundle], need: DisplayFirmwareNeed, device: bool, firmware: int, sync: list[str]
) -> None:
    deps, adapters = env
    adapters.app_environment.display_firmware_need = need
    if not device:
        record = adapters.state.read_apps_state().apps[APP_ID]
        assert record.manifest is not None
        record = replace(record, manifest={**record.manifest, "devices": []})
        adapters.state.write_apps_state(AppsState(apps={APP_ID: record}))
    start_app(deps, APP_ID, host="robot")
    assert adapters.platform.display_firmware_calls == firmware
    assert adapters.platform.sync_display_assets_calls == sync
    assert len(adapters.platform.desire_display_firmware_calls) == firmware
    assert APP_ID in adapters.app_unit.start_calls
    assert adapters.state.read_apps_state().apps[APP_ID].display_firmware == need


@pytest.mark.parametrize(
    ("outcome", "code", "launch"),
    [
        ("updated", None, True),
        ("current", None, True),
        ("absent", None, True),
        ("undeclared", None, True),
        ("failed", "display_firmware_failed", False),
        ("undesired", "display_firmware_failed", False),
        ("busy", "display_busy", False),
    ],
)
def test_start_handles_firmware_results(
    env: tuple[StartDeps, FakeAdapterBundle], outcome: str, code: str | None, launch: bool
) -> None:
    deps, adapters = env
    adapters.platform.display_firmware_result = DisplayFirmwareResult.from_payload(
        {"result": outcome, "reason": "detail"}
    )
    if code:
        with pytest.raises(AppStartError) as caught:
            start_app(deps, APP_ID, host="robot")
        assert caught.value.code == code
    else:
        start_app(deps, APP_ID, host="robot")
    assert (APP_ID in adapters.app_unit.start_calls) == launch
    assert bool(adapters.platform.sync_display_assets_calls) == (outcome in {"updated", "current"})


@pytest.mark.parametrize(
    ("outcome", "deleted"),
    [("synced", ()), ("skipped", ()), ("busy", ()), ("failed", ()), ("failed", ("palmimo/system.idle",))],
)
def test_start_sync_records_pending_for_busy_or_destructive_failure(
    env: tuple[StartDeps, FakeAdapterBundle], outcome: str, deleted: tuple[str, ...]
) -> None:
    deps, adapters = env
    pending = DisplaySyncPending(True, "old", "old-boot")
    adapters.state.write_display_sync(pending)
    adapters.platform.display_sync_result = DisplaySyncResult.from_payload(
        {"result": outcome, "reason": "new", "deleted": list(deleted)}
    )
    if outcome in {"busy", "failed"}:
        with pytest.raises(AppStartError) as caught:
            start_app(deps, APP_ID, host="robot")
        assert caught.value.code == ("display_busy" if outcome == "busy" else "display_sync_failed")
    else:
        start_app(deps, APP_ID, host="robot")
    expected = (
        None
        if outcome == "synced"
        else DisplaySyncPending(True, "new", "boot-a")
        if outcome == "busy" or (outcome == "failed" and deleted)
        else pending
    )
    assert adapters.state.read_display_sync() == expected


def test_start_releases_display_before_desire_and_writes_asset_vocabulary(
    env: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch
) -> None:
    deps, adapters = env
    events = []
    original = adapters.platform.desire_display_firmware

    def desire(protocol: DisplayProtocol, by: Literal["app-start", "initial"]) -> DisplayDesireResult:
        events.append("desire")
        return original(protocol, by)

    monkeypatch.setattr(adapters.platform, "desire_display_firmware", desire)
    start_app(replace(deps, before_start=lambda devices: events.append("release")), APP_ID, host="robot")
    assert events == ["release", "desire"]
    assert json.loads(adapters.run_dir.written[APP_ID]["assets"]) == {
        "schema": 1,
        "assets": [
            {
                "id": f"{APP_ID}/welcome",
                "description": "Welcome",
                "tags": ["greeting"],
                "params": {"eye": {"description": "Eye"}},
                "translations": {"ja": {"description": "Translated welcome", "params.eye": "Translated eye"}},
            }
        ],
    }


@pytest.mark.parametrize("stage", ["firmware", "assets"])
@pytest.mark.parametrize("busy", [False, True])
def test_restore_launches_after_display_failure_but_preserves_busy(
    env: tuple[StartDeps, FakeAdapterBundle], stage: str, busy: bool
) -> None:
    deps, adapters = env
    if stage == "firmware":
        adapters.platform.display_firmware_result = DisplayFirmwareResult("busy" if busy else "failed")
    else:
        adapters.platform.display_sync_result = DisplaySyncResult("busy" if busy else "failed", (), (), None)
    if busy:
        with pytest.raises(AppStartError) as caught:
            start_app(deps, APP_ID, host="robot", origin="restore")
        assert caught.value.code == "display_busy"
    else:
        start_app(deps, APP_ID, host="robot", origin="restore")
    assert (APP_ID in adapters.app_unit.start_calls) != busy


@pytest.mark.parametrize("outcome", ["current", "absent", "undeclared", "failed", "busy"])
def test_start_job_records_terminal_result_and_releases_locks(
    env: tuple[StartDeps, FakeAdapterBundle], outcome: str
) -> None:
    from palmimo_portal.core.apps_start_job import StartJobRunner

    deps, adapters = env
    adapters.platform.display_firmware_result = DisplayFirmwareResult.from_payload({"result": outcome})
    runner = StartJobRunner(deps, run_in_thread=False)
    job = runner.start(APP_ID, host="robot", origin="api")
    state = adapters.state.read_apps_state()
    assert state.current_job is None
    assert state.current_job_app is None
    assert state.apps[APP_ID].last_job == job
    assert job.kind == "start" and job.boot_id == "boot-a"
    assert job.state == ("failed" if outcome in {"failed", "busy"} else "done")
    assert job.warning == (outcome if outcome in {"absent", "undeclared"} else None)
    assert not runner.is_alive()
    with adapters.state.lock_apps(), adapters.state.lock_run():
        pass


@pytest.mark.parametrize("origin", ["api", "autostart", "resume"])
def test_start_job_releases_before_recording_only_for_api(
    env: tuple[StartDeps, FakeAdapterBundle], origin: Literal["api", "autostart", "resume"]
) -> None:
    from palmimo_portal.core.apps_start_job import StartJobRunner

    deps, adapters = env
    events = []

    def release(devices: list[str]) -> None:
        assert adapters.state.read_apps_state().current_job is None
        events.append("release")

    runner = StartJobRunner(replace(deps, before_start=release), run_in_thread=False)
    assert runner.start(APP_ID, host="robot", origin=origin).state == "done"
    assert events == (["release"] if origin == "api" else [])


def test_start_job_holds_both_locks_and_rejects_stop_during_firmware(
    env: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from palmimo_portal.core.apps_start import AppBusyError, AppJobInProgressStartError, stop_app
    from palmimo_portal.core.apps_start_job import StartJobRunner
    from palmimo_portal.ports import AppsLockTimeoutError, RunLockTimeoutError

    deps, adapters = env
    entered, release = threading.Event(), threading.Event()

    def ensure() -> DisplayFirmwareResult:
        entered.set()
        assert release.wait(3)
        return DisplayFirmwareResult("current")

    monkeypatch.setattr(adapters.platform, "ensure_display_firmware", ensure)
    runner = StartJobRunner(deps)
    job = runner.start(APP_ID, host="robot", origin="api")
    try:
        assert entered.wait(2)
        assert job.state == "running" and job.step == "firmware"
        assert runner.is_alive()
        with pytest.raises(AppsLockTimeoutError), adapters.state.lock_apps():
            pass
        with pytest.raises(RunLockTimeoutError), adapters.state.lock_run():
            pass
        with pytest.raises(AppBusyError):
            stop_app(deps, APP_ID)
        with pytest.raises(AppJobInProgressStartError):
            runner.start(APP_ID, host="robot", origin="api")
        assert runner.is_alive()
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while runner.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
    assert not runner.is_alive()
    last_job = adapters.state.read_apps_state().apps[APP_ID].last_job
    assert last_job is not None and last_job.state == "done"


def test_start_job_precheck_failure_does_not_record_or_hold_locks(env: tuple[StartDeps, FakeAdapterBundle]) -> None:
    from palmimo_portal.core.apps_start_job import StartJobRunner

    deps, adapters = env
    runner = StartJobRunner(replace(deps, platform_ready=lambda: False), run_in_thread=False)
    with pytest.raises(AppStartError) as caught:
        runner.start(APP_ID, host="robot", origin="api")
    assert caught.value.code == "platform_not_ready"
    assert adapters.state.read_apps_state().current_job is None
    assert not runner.is_alive()
    with adapters.state.lock_apps(), adapters.state.lock_run():
        pass


@pytest.mark.parametrize("busy", [False, True])
def test_wifi_restore_retains_preempted_record_only_for_busy_display(
    env: tuple[StartDeps, FakeAdapterBundle], busy: bool
) -> None:
    from palmimo_portal.core.wifi_setup import WifiSetupApps
    from palmimo_portal.ports import PreemptedApp

    deps, adapters = env
    adapters.state.write_preempted_app(PreemptedApp(APP_ID, "wifi-setup"))
    adapters.platform.display_firmware_result = DisplayFirmwareResult("busy" if busy else "failed")
    assert WifiSetupApps(deps, hostname="robot").restore(hold=False) != busy
    assert (adapters.state.read_preempted_app() is not None) == busy
    assert bool(adapters.app_unit.start_calls) != busy


def test_dead_start_runner_does_not_block_power_after_terminal_write_failure(
    env: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch
) -> None:
    from palmimo_portal.api.errors import PortalError
    from palmimo_portal.api.system import _refuse_while_updating
    from palmimo_portal.core.apps_start_job import StartJobRunner

    deps, adapters = env
    original = adapters.state.update_apps_state

    def update(fn: Callable[[AppsState], AppsState]) -> AppsState:
        state = fn(adapters.state.read_apps_state())
        if state.current_job is None:
            raise OSError("state unavailable")
        return original(lambda previous: state)

    monkeypatch.setattr(adapters.state, "update_apps_state", update)
    adapters.platform.display_firmware_result = DisplayFirmwareResult("failed")
    runner = StartJobRunner(deps, run_in_thread=False)
    assert runner.start(APP_ID, host="robot", origin="api").state == "failed"
    assert not runner.is_alive()
    job = adapters.state.read_apps_state().current_job
    assert job is not None and job.step == "firmware"
    _refuse_while_updating(adapters.state, False, False, runner.is_alive())
    with pytest.raises(PortalError) as caught:
        _refuse_while_updating(adapters.state, False, False, True)
    assert caught.value.status_code == 409
    detail = caught.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "platform_update_in_progress"


def test_start_job_release_refusal_is_synchronous_and_leaves_no_job(env: tuple[StartDeps, FakeAdapterBundle]) -> None:
    from palmimo_portal.core.apps_start import AppBusyError
    from palmimo_portal.core.apps_start_job import StartJobRunner

    deps, adapters = env

    def release(devices: list[str]) -> None:
        raise AppBusyError()

    runner = StartJobRunner(replace(deps, before_start=release), run_in_thread=False)
    with pytest.raises(AppBusyError):
        runner.start(APP_ID, host="robot", origin="api")
    assert adapters.state.read_apps_state().current_job is None
    assert not adapters.platform.desire_display_firmware_calls
    assert not runner.is_alive()
    with adapters.state.lock_apps(), adapters.state.lock_run():
        pass


def test_start_job_thread_creation_failure_records_failure_and_releases_locks(
    env: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from palmimo_portal.core.apps_start_job import StartJobRunner

    deps, adapters = env

    def spawn_failed(thread: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", spawn_failed)
    runner = StartJobRunner(deps)
    with pytest.raises(RuntimeError):
        runner.start(APP_ID, host="robot", origin="api")
    state = adapters.state.read_apps_state()
    assert state.current_job is None
    last_job = state.apps[APP_ID].last_job
    assert last_job is not None and last_job.error_code == "start_failed"
    assert not runner.is_alive()
    assert not adapters.platform.desire_display_firmware_calls
    with adapters.state.lock_apps(), adapters.state.lock_run():
        pass


def test_start_launch_write_failure_removes_partial_run_directory(
    env: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch
) -> None:
    deps, adapters = env
    original = adapters.run_dir.write

    def write(name: str, **kwargs: object) -> None:
        original(name, **kwargs)  # type: ignore[arg-type]
        raise OSError("write interrupted")

    monkeypatch.setattr(adapters.run_dir, "write", write)
    with pytest.raises(OSError):
        start_app(deps, APP_ID, host="robot")
    assert APP_ID not in adapters.run_dir.written
    assert not adapters.app_unit.start_calls


def test_start_job_records_safe_failure_when_secret_collection_is_unavailable(
    env: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch
) -> None:
    from palmimo_portal.core.apps_start_job import StartJobRunner
    from palmimo_portal.ports import SecretRecord

    deps, adapters = env
    adapters.app_unit.raise_on_start = RuntimeError("sensitive exception detail")

    def unavailable() -> list[SecretRecord]:
        raise OSError("secret metadata unavailable")

    monkeypatch.setattr(adapters.secrets, "list_secrets", unavailable)
    job = StartJobRunner(deps, run_in_thread=False).start(APP_ID, host="robot", origin="api")
    assert job.state == "failed" and job.error_code == "start_failed"
    assert job.error == "start_failed"
    assert adapters.state.read_apps_state().current_job is None


@pytest.mark.parametrize("active_state", ["active", "activating", "deactivating"])
def test_resume_of_running_unit_finishes_without_display_or_launch(
    env: tuple[StartDeps, FakeAdapterBundle], active_state: str
) -> None:
    from palmimo_portal.core.apps_start_job import StartJobRunner
    from palmimo_portal.ports import UnitStatus

    deps, adapters = env
    adapters.app_unit.statuses[APP_ID] = UnitStatus(active_state, "running", "success", 0)
    adapters.platform.display_sync_result = DisplaySyncResult("busy", (), (), "app owns display")
    pending = DisplaySyncPending(True, "old", "old-boot")
    adapters.state.write_display_sync(pending)
    job = StartJobRunner(deps, run_in_thread=False).start(APP_ID, host="robot", origin="resume")
    assert job.state == "done" and job.step == "launch"
    assert job.warning is None and job.error_code is None
    assert not adapters.platform.desire_display_firmware_calls
    assert adapters.platform.display_firmware_calls == 0
    assert not adapters.platform.sync_display_assets_calls
    assert not adapters.app_unit.start_calls
    assert not adapters.app_unit.device_allow_calls
    assert APP_ID not in adapters.run_dir.written
    assert adapters.state.read_display_sync() == pending
    assert adapters.state.read_apps_state().current_job is None


@pytest.mark.parametrize("origin", ["api", "autostart", "resume", "restore"])
def test_start_metadata_write_failure_does_not_prevent_launch(
    env: tuple[StartDeps, FakeAdapterBundle],
    monkeypatch: pytest.MonkeyPatch,
    origin: Literal["api", "autostart", "resume", "restore"],
) -> None:
    deps, adapters = env

    def unavailable(fn: Callable[[AppsState], AppsState]) -> AppsState:
        raise OSError("ledger write temporarily unavailable")

    monkeypatch.setattr(adapters.state, "update_apps_state", unavailable)
    start_app(deps, APP_ID, host="robot", origin=origin)
    assert adapters.app_unit.start_calls == [APP_ID]
    assert adapters.platform.display_firmware_calls == 1
    assert adapters.platform.sync_display_assets_calls == [APP_ID]
    assert adapters.state.read_apps_state().apps[APP_ID].display_firmware is None


@pytest.mark.parametrize("origin", ["api", "autostart", "resume", "restore"])
@pytest.mark.parametrize("outcome", ["synced", "busy"])
def test_start_sync_record_write_failure_preserves_sync_outcome(
    env: tuple[StartDeps, FakeAdapterBundle],
    monkeypatch: pytest.MonkeyPatch,
    origin: Literal["api", "autostart", "resume", "restore"],
    outcome: Literal["synced", "busy"],
) -> None:
    deps, adapters = env
    pending = DisplaySyncPending(True, "old", "old-boot")
    adapters.state.write_display_sync(pending)
    adapters.platform.display_sync_result = DisplaySyncResult(outcome, (), (), "app owns display")

    def unavailable(pending: DisplaySyncPending | None) -> None:
        raise OSError("pending state unavailable")

    monkeypatch.setattr(adapters.state, "write_display_sync", unavailable)
    if outcome == "busy":
        with pytest.raises(AppStartError) as caught:
            start_app(deps, APP_ID, host="robot", origin=origin)
        assert caught.value.code == "display_busy"
        assert caught.value.params["reason"] == "app owns display"
        assert not adapters.app_unit.start_calls
    else:
        start_app(deps, APP_ID, host="robot", origin=origin)
        assert adapters.app_unit.start_calls == [APP_ID]
    assert adapters.state.read_display_sync() == pending


@pytest.mark.parametrize("failure", ["metadata", "synced", "busy"])
def test_wifi_restore_record_write_failure_does_not_discard_retry_or_block_launch(
    env: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from palmimo_portal.core.wifi_setup import WifiSetupApps
    from palmimo_portal.ports import PreemptedApp

    deps, adapters = env
    preempted = PreemptedApp(APP_ID, "wifi-setup")
    adapters.state.write_preempted_app(preempted)
    if failure == "metadata":

        def update_unavailable(fn: Callable[[AppsState], AppsState]) -> AppsState:
            raise OSError("ledger write temporarily unavailable")

        monkeypatch.setattr(adapters.state, "update_apps_state", update_unavailable)
    else:

        def pending_unavailable(pending: DisplaySyncPending | None) -> None:
            raise OSError("pending state unavailable")

        monkeypatch.setattr(adapters.state, "write_display_sync", pending_unavailable)
        adapters.platform.display_sync_result = DisplaySyncResult.from_payload({"result": failure})
    restored = WifiSetupApps(deps, hostname="robot").restore(hold=False)
    assert restored == (failure != "busy")
    assert adapters.state.read_preempted_app() == (preempted if failure == "busy" else None)
    assert adapters.app_unit.start_calls == ([] if failure == "busy" else [APP_ID])


@pytest.mark.parametrize("failure", ["metadata", "pending"])
def test_start_job_record_write_failure_still_records_done(
    env: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from palmimo_portal.core.apps_start_job import StartJobRunner

    deps, adapters = env
    original = adapters.state.update_apps_state
    if failure == "metadata":

        def update(fn: Callable[[AppsState], AppsState]) -> AppsState:
            before = adapters.state.read_apps_state()
            after = fn(before)
            if after.apps[APP_ID].display_firmware != before.apps[APP_ID].display_firmware:
                raise OSError("ledger write temporarily unavailable")
            return original(lambda previous: after)

        monkeypatch.setattr(adapters.state, "update_apps_state", update)
    else:

        def unavailable(pending: DisplaySyncPending | None) -> None:
            raise OSError("pending state unavailable")

        monkeypatch.setattr(adapters.state, "write_display_sync", unavailable)
    job = StartJobRunner(deps, run_in_thread=False).start(APP_ID, host="robot", origin="api")
    state = adapters.state.read_apps_state()
    assert job.state == "done" and job.warning is None
    assert state.current_job is None and state.apps[APP_ID].last_job == job
    assert adapters.app_unit.start_calls == [APP_ID]
