import subprocess
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.adapters.platform import SudoPlatformPort
from palmimo_portal.api.app import create_app
from palmimo_portal.core.apps_start import StartDeps
from palmimo_portal.core.display_firmware import restore_preempted_app, run_display_firmware
from palmimo_portal.core.platform import PlatformUpdateRunner
from palmimo_portal.ports import AppRecord, AppSource, AppsState, DisplayFirmwareResult, PreemptedApp, UnitStatus
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[StartDeps, FakeAdapterBundle]:
    settings = Settings(apps_dir=tmp_path / "apps", platform_dir=tmp_path / "platform", periodic_enabled=False)
    app = create_app(settings)
    adapters: FakeAdapterBundle = app.state.adapters
    path = settings.apps_dir / "face"
    (path / ".venv" / "bin").mkdir(parents=True)
    (path / ".venv" / "bin" / "python").touch()
    (path / "palmimo.toml").write_text(
        'schema = 1\nname = "face"\ndescription = "d"\ncommand = ["run"]\ndevices = ["motor_display"]\n'
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
    adapters.platform.display_firmware_available = True
    adapters.app_unit.statuses["face"] = UnitStatus("active", "running", "success", 0)
    return app.state.start_deps, adapters


@pytest.mark.parametrize(
    ("failure", "reason"),
    [("exit78", "configuration invalid"), ("timeout", "timed out"), ("json", "invalid tool output")],
)
def test_display_firmware_failure_finishes_update_and_restores_app(
    prepared: tuple[StartDeps, FakeAdapterBundle],
    tmp_path: Path,
    failure: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps, adapters = prepared

    def execute(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 660)
        return subprocess.CompletedProcess(
            argv,
            78 if failure == "exit78" else 0,
            stdout='{"result":"failed","reason":"configuration invalid"}' if failure == "exit78" else "broken JSON",
            stderr="",
        )

    tool = SudoPlatformPort(installed_path=tmp_path / "installed.json", runner=execute)
    monkeypatch.setattr(adapters.platform, "ensure_display_firmware", tool.ensure_display_firmware)
    adapters.platform_bundle.manifest = {
        "version": 2,
        "requires_portal": "0.0.0",
        "restart_portal": False,
        "summary": "s",
    }
    runner = PlatformUpdateRunner(
        adapters.state,
        adapters.platform_bundle,
        adapters.platform,
        adapters.system,
        tmp_path,
        portal_installed_version="1.0.0",
        display_firmware=lambda: run_display_firmware(deps, adapters.platform),
    )
    runner.run("v2", 2)
    state = adapters.state.read_platform_update_state()
    assert state.job.state == "done"
    assert state.display_firmware is not None
    assert state.display_firmware.result == "failed"
    assert reason in (state.display_firmware.reason or "")
    assert adapters.app_unit.stop_calls == ["face"]
    assert adapters.app_unit.start_calls == ["face"]
    assert adapters.state.read_preempted_app() is None


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("by", ["display-firmware", "wifi-setup"])
def test_preempted_app_recovery_obeys_network_state(
    prepared: tuple[StartDeps, FakeAdapterBundle],
    connected: bool,
    by: str,
) -> None:
    deps, adapters = prepared
    adapters.app_unit.statuses.clear()
    record = PreemptedApp(preempted_app="face", by=by, hold=False)
    adapters.state.write_preempted_app(record)
    assert restore_preempted_app(deps, connected=connected)
    assert adapters.app_unit.start_calls == (["face"] if connected else [])
    assert adapters.state.read_preempted_app() == (None if connected else record)


def test_preempted_running_app_is_not_restarted(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    adapters.state.write_preempted_app(PreemptedApp("face", "display-firmware", False))
    restore_preempted_app(deps, connected=True)
    assert adapters.app_unit.start_calls == []
    assert adapters.app_unit.device_allow_calls == []
    assert adapters.state.read_preempted_app() is None


def test_display_firmware_does_not_restart_app_when_stop_failed(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    adapters.app_unit.raise_on_stop = RuntimeError("stop unavailable")
    result = run_display_firmware(deps, adapters.platform)
    assert result.result == "failed"
    assert adapters.platform.display_firmware_calls == 0
    assert adapters.app_unit.start_calls == []
    assert adapters.app_unit.device_allow_calls == []
    assert adapters.state.read_preempted_app() is None


def test_display_firmware_retains_record_when_restore_fails(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    adapters.app_unit.raise_on_start = RuntimeError("start unavailable")
    result = run_display_firmware(deps, adapters.platform)
    assert result == DisplayFirmwareResult(result="current")
    assert adapters.state.read_preempted_app() == PreemptedApp("face", "display-firmware", False)


def test_display_firmware_skips_missing_tool(prepared: tuple[StartDeps, FakeAdapterBundle], tmp_path: Path) -> None:
    deps, adapters = prepared
    adapters.platform.display_firmware_available = False
    adapters.platform_bundle.manifest = {
        "version": 2,
        "requires_portal": "0.0.0",
        "restart_portal": False,
        "summary": "s",
    }
    runner = PlatformUpdateRunner(
        adapters.state,
        adapters.platform_bundle,
        adapters.platform,
        adapters.system,
        tmp_path,
        portal_installed_version="1.0.0",
        display_firmware=lambda: run_display_firmware(deps, adapters.platform),
    )
    runner.run("v2", 2)
    assert adapters.state.read_platform_update_state().job.state == "done"
    assert adapters.state.read_platform_update_state().display_firmware is None
    assert adapters.platform.display_firmware_calls == 0
    assert adapters.app_unit.stop_calls == []


def test_display_firmware_refuses_run_lock_contention(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    with adapters.state.lock_run():
        result = run_display_firmware(deps, adapters.platform)
    assert result.result == "busy"
    assert adapters.platform.display_firmware_calls == 0
    assert adapters.app_unit.stop_calls == []


def test_display_firmware_leaves_existing_preemption_untouched(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    record = PreemptedApp("face", "wifi-setup", False)
    adapters.state.write_preempted_app(record)
    result = run_display_firmware(deps, adapters.platform)
    assert result.result == "busy"
    assert adapters.state.read_preempted_app() == record
    assert adapters.platform.display_firmware_calls == 0
    assert adapters.app_unit.stop_calls == []
    assert adapters.app_unit.start_calls == []


def test_display_firmware_does_not_flash_when_app_never_stops(
    prepared: tuple[StartDeps, FakeAdapterBundle],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from palmimo_portal.core import display_firmware

    deps, adapters = prepared
    clock = iter((0.0, 21.0))
    monkeypatch.setattr(display_firmware.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(adapters.app_unit, "stop", lambda name: None)
    result = run_display_firmware(deps, adapters.platform)
    assert result.result == "busy"
    assert adapters.platform.display_firmware_calls == 0
    assert adapters.app_unit.start_calls == []
    assert adapters.app_unit.device_allow_calls == []
    assert adapters.state.read_preempted_app() is None


def test_preempt_hold_suppresses_autostart_until_connected(prepared: tuple[StartDeps, FakeAdapterBundle]) -> None:
    deps, adapters = prepared
    record = PreemptedApp(None, "wifi-setup", True)
    adapters.state.write_preempted_app(record)
    assert restore_preempted_app(deps, connected=False)
    assert adapters.state.read_preempted_app() == record
    assert not restore_preempted_app(deps, connected=True)
    assert adapters.state.read_preempted_app() is None
    assert adapters.app_unit.start_calls == []
