import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.adapters.state import JsonFileStateStore
from palmimo_portal.api.app import create_app
from palmimo_portal.core.apps_start import AppBusyError, PlatformNotReadyError, StartDeps, start_app
from palmimo_portal.core.display_firmware import restore_preempted_app
from palmimo_portal.core.wifi_setup import WifiSetupApps
from palmimo_portal.ports import AppRecord, AppSource, AppsState, PreemptedApp, UnitStatus
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[StartDeps, FakeAdapterBundle]:
    app = create_app(Settings(apps_dir=tmp_path / "apps", periodic_enabled=False))
    deps: StartDeps = replace(app.state.start_deps, platform_ready=lambda: True)
    adapters: FakeAdapterBundle = app.state.adapters
    path = deps.apps_job_ctx.app_dir("face")
    (path / ".venv" / "bin").mkdir(parents=True)
    (path / ".venv" / "bin" / "python").touch()
    (path / "palmimo.toml").write_text(
        'schema = 1\nname = "face"\ndescription = "d"\ncommand = ["run"]\ndevices = ["camera"]\n'
    )
    adapters.state.write_apps_state(
        AppsState(
            apps={
                "face": AppRecord(
                    name="face",
                    id="face",
                    source=AppSource(type="zip"),
                    installed_at=0,
                    params={},
                    autostart=False,
                    last_job=None,
                ),
            }
        )
    )
    return deps, adapters


@pytest.mark.parametrize("app,hold", [("face", False), ("face", True), (None, True)])
def test_wifi_setup_record_survives_restart(tmp_path: Path, app: str | None, hold: bool) -> None:
    path = tmp_path / "preempt.json"
    store = JsonFileStateStore(tmp_path / "state", preempt_path=path)
    record = PreemptedApp(app, "wifi-setup", hold=hold)
    store.write_preempted_app(record)
    assert JsonFileStateStore(tmp_path / "state", preempt_path=path).read_preempted_app() == record


@pytest.mark.parametrize("hold", [None, "false", 0, [], {}])
def test_wifi_setup_record_rejects_invalid_hold(tmp_path: Path, hold: Any) -> None:
    path = tmp_path / "preempt.json"
    path.write_text(json.dumps({"preempted_app": "face", "by": "wifi-setup", "hold": hold}))
    assert JsonFileStateStore(tmp_path / "state", preempt_path=path).read_preempted_app() is None


def test_preempt_record_reads_legacy_without_hold(tmp_path: Path) -> None:
    path = tmp_path / "preempt.json"
    path.write_text('{"preempted_app":"face","by":"display-firmware"}')
    assert JsonFileStateStore(tmp_path / "state", preempt_path=path).read_preempted_app() == PreemptedApp(
        "face",
        "display-firmware",
        hold=False,
    )


def test_wifi_setup_records_app_before_stopping(
    prepared: tuple[StartDeps, FakeAdapterBundle],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps, adapters = prepared
    adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    stopped: list[str] = []

    def stop(name: str) -> None:
        assert adapters.state.read_preempted_app() == PreemptedApp("face", "wifi-setup")
        stopped.append(name)

    monkeypatch.setattr(adapters.app_unit, "stop", stop)
    with deps.state.lock_run():
        assert WifiSetupApps(deps).prepare_locked()
    assert stopped == ["face"]


def test_wifi_setup_keeps_app_running_when_record_write_fails(
    prepared: tuple[StartDeps, FakeAdapterBundle],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps, adapters = prepared
    adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))

    def fail(record: PreemptedApp | None) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(adapters.state, "write_preempted_app", fail)
    with deps.state.lock_run():
        assert not WifiSetupApps(deps).prepare_locked()
    assert adapters.app_unit.stop_calls == []


@pytest.mark.parametrize("active", ["active", "activating", "deactivating"])
def test_wifi_setup_restore_does_not_restart_running_unit(
    prepared: tuple[StartDeps, FakeAdapterBundle],
    active: str,
) -> None:
    deps, adapters = prepared
    adapters.state.write_preempted_app(PreemptedApp("face", "wifi-setup"))
    adapters.app_unit.simulate_active_state("face", UnitStatus(active, "", "success", 0))
    assert WifiSetupApps(deps).restore(hold=False)
    assert adapters.app_unit.start_calls == []
    assert adapters.app_unit.device_allow_calls == []
    assert adapters.state.read_preempted_app() is None


@pytest.mark.parametrize("failure,retained", [(AppBusyError(), True), (RuntimeError("failed"), False)])
def test_wifi_setup_restore_retains_only_retryable_failures(
    prepared: tuple[StartDeps, FakeAdapterBundle],
    failure: Exception,
    retained: bool,
) -> None:
    deps, adapters = prepared
    adapters.state.write_preempted_app(PreemptedApp("face", "wifi-setup"))
    adapters.app_unit.raise_on_start = failure
    assert WifiSetupApps(deps).restore(hold=False) is not retained
    assert (adapters.state.read_preempted_app() is not None) is retained


def test_wifi_setup_restore_rechecks_record_after_locking(
    prepared: tuple[StartDeps, FakeAdapterBundle],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps, adapters = prepared
    coordinator = WifiSetupApps(deps)
    adapters.state.write_preempted_app(PreemptedApp("face", "wifi-setup"))
    original = adapters.state.lock_run

    @contextmanager
    def lock_run(timeout_s: float = 0.0) -> Iterator[None]:
        with original(timeout_s):
            coordinator.hold_locked()
            yield

    monkeypatch.setattr(adapters.state, "lock_run", lock_run)
    assert coordinator.restore(hold=False)
    assert adapters.app_unit.start_calls == []
    assert adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)


def test_wifi_setup_restores_app_and_holds_after_reader_failure(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    adapters.state.write_preempted_app(PreemptedApp("face", "wifi-setup"))
    assert WifiSetupApps(deps).restore(hold=True)
    assert adapters.app_unit.start_calls == ["face"]
    assert adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)


def test_wifi_setup_preserves_display_firmware_record(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    record = PreemptedApp("face", "display-firmware")
    adapters.state.write_preempted_app(record)
    with deps.state.lock_run():
        assert not WifiSetupApps(deps).prepare_locked()
    assert adapters.state.read_preempted_app() == record


def test_app_start_runs_setup_hook_after_prechecks(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    devices: list[list[str]] = []

    def before_start(value: list[str]) -> None:
        assert adapters.app_unit.start_calls == []
        assert adapters.run_dir.written == {}
        devices.append(value)

    start_app(replace(deps, before_start=before_start), "face", host="robot")
    assert devices == [["camera"]]


def test_app_start_rejection_does_not_call_setup_hook(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, _ = prepared
    calls: list[list[str]] = []
    with pytest.raises(PlatformNotReadyError):
        start_app(replace(deps, platform_ready=lambda: False, before_start=calls.append), "face", host="robot")
    assert calls == []


def test_display_recovery_bypasses_wifi_setup_start_hook(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    calls: list[list[str]] = []
    adapters.state.write_preempted_app(PreemptedApp("face", "display-firmware"))
    assert restore_preempted_app(replace(deps, before_start=calls.append))
    assert adapters.app_unit.start_calls == ["face"]
    assert calls == []
