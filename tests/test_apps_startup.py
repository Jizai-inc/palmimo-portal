import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api.app import create_app
from palmimo_portal.ports import AppJob, AppRecord, AppSource, DisplayFirmwareResult, PreemptedApp
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


@pytest.fixture
def startup_app(tmp_path: Path) -> FastAPI:
    platform = tmp_path / "platform"
    (platform / "current").mkdir(parents=True)
    (platform / "current" / "install.sh").touch()
    app = create_app(
        Settings(
            allowed_hosts=frozenset({"testserver"}),
            apps_dir=tmp_path / "apps",
            platform_dir=platform,
            periodic_enabled=False,
            start_run_in_thread=False,
            startup_run_in_thread=False,
        )
    )

    app.state.adapters.app_environment.display_firmware_need = "2"
    return app


def install(app: FastAPI, name: str, *, auto: bool = False, display: bool = True) -> None:
    project = app.state.apps_job_context.app_dir(name)
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    (project / "palmimo.toml").write_text(
        f'schema=1\nname="{name}"\ndescription="d"\ncommand=["run"]\ndevices={["motor_display"] if display else []!s}\n'
    )
    record = AppRecord(name, AppSource("zip"), 0, {}, auto, None, id=name)
    app.state.adapters.state.update_apps_state(lambda state: replace(state, apps={**state.apps, name: record}))


@pytest.mark.parametrize(
    ("saved", "current", "expected"),
    [("a", "a", "resume"), ("a", "b", "auto"), (None, "a", "auto"), ("a", None, "auto"), (None, None, "auto")],
)
def test_startup_resumes_only_same_known_boot_and_suppresses_autostart(
    startup_app: FastAPI, saved: str | None, current: str | None, expected: str
) -> None:
    install(startup_app, "resume")
    install(startup_app, "auto", auto=True)
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.platform.boot_id = current
    job = AppJob("interrupted", "start", "running", "firmware", None, 0, None, boot_id=saved)
    adapters.state.update_apps_state(lambda state: replace(state, current_job=job, current_job_app="resume"))
    with TestClient(startup_app):
        assert adapters.app_unit.start_calls == [expected]
        state = adapters.state.read_apps_state()
        assert state.current_job is None
        last_job = state.apps[expected].last_job
        assert last_job is not None
        assert last_job.state == "done"
        if expected != "resume":
            interrupted = state.apps["resume"].last_job
            assert interrupted is not None
            assert interrupted.error == "interrupted"


def test_startup_backfills_only_unknown_requirements_with_existing_venvs(startup_app: FastAPI) -> None:
    install(startup_app, "unknown")
    install(startup_app, "known")
    install(startup_app, "missing")
    adapters: FakeAdapterBundle = startup_app.state.adapters
    shutil.rmtree(startup_app.state.apps_job_context.app_dir("missing") / ".venv")
    adapters.state.update_apps_state(
        lambda state: replace(state, apps={**state.apps, "known": replace(state.apps["known"], display_firmware="1")})
    )
    adapters.app_environment.display_firmware_need = "2"
    with TestClient(startup_app):
        apps = adapters.state.read_apps_state().apps
        assert apps["unknown"].display_firmware == "2"
        assert apps["known"].display_firmware == "1"
        assert apps["missing"].display_firmware is None


def test_startup_skips_display_desire_when_platform_not_ready(startup_app: FastAPI) -> None:
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.platform.installed = None
    with TestClient(startup_app):
        assert adapters.platform.desire_display_firmware_calls == []
        assert adapters.platform.display_firmware_calls == 0


def test_startup_falls_back_to_maintenance_when_autostart_precheck_fails(startup_app: FastAPI) -> None:
    install(startup_app, "auto", auto=True)
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.app_environment.display_firmware_need = "1"
    (startup_app.state.apps_job_context.app_dir("auto") / "palmimo.toml").write_text(
        'schema=1\nname="auto"\ndescription="d"\ncommand=["run"]\ndevices=["motor_display"]\n[env.KEY]\ndescription="required"\n'
    )
    with TestClient(startup_app):
        assert adapters.app_unit.start_calls == []
        assert adapters.platform.display_firmware_calls == 1
        assert adapters.platform.desire_display_firmware_calls == [(1, "initial")]


def test_startup_serves_http_during_ensure_and_keeps_monitor_stopped_after_shutdown(
    startup_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(startup_app, "auto", auto=True)
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.app_environment.display_firmware_need = "2"
    startup_app.state.settings = replace(
        startup_app.state.settings, startup_run_in_thread=True, wifi_setup_enabled=True
    )
    entered = threading.Event()
    release = threading.Event()
    monitor_started = threading.Event()
    monkeypatch.setattr(startup_app.state.wifi_setup, "run_forever", monitor_started.set)

    def ensure() -> DisplayFirmwareResult:
        entered.set()
        assert release.wait(5)
        return DisplayFirmwareResult("current")

    monkeypatch.setattr(adapters.platform, "ensure_display_firmware", ensure)
    try:
        with TestClient(startup_app) as client:
            assert entered.wait(3)
            assert client.get("/api/v1/nonexistent").status_code == 404
    finally:
        release.set()
        startup_app.state.startup_thread.join(3)
    assert not startup_app.state.startup_thread.is_alive()
    assert not monitor_started.is_set()
    last_job = adapters.state.read_apps_state().apps["auto"].last_job
    assert last_job is not None and last_job.state == "done"


def test_wifi_monitor_starts_after_preempted_app_recovery(
    startup_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(startup_app, "face")
    startup_app.state.settings = replace(startup_app.state.settings, wifi_setup_enabled=True)
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.state.write_preempted_app(PreemptedApp("face", "display-firmware"))
    observed: list[list[str]] = []
    monitor = threading.Event()

    def run() -> None:
        observed.append(list(adapters.app_unit.start_calls))
        monitor.set()

    monkeypatch.setattr(startup_app.state.wifi_setup, "run_forever", run)
    with TestClient(startup_app):
        assert monitor.wait(3)
        assert observed == [["face"]]


def test_preempt_recovery_suppresses_interrupted_resume_and_autostart(startup_app: FastAPI) -> None:
    for name in ("face", "resume", "auto"):
        install(startup_app, name, auto=name == "auto")
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.platform.boot_id = "boot"
    job = AppJob("interrupted", "start", "running", "firmware", None, 0, None, boot_id="boot")
    adapters.state.update_apps_state(lambda state: replace(state, current_job=job, current_job_app="resume"))
    adapters.state.write_preempted_app(PreemptedApp("face", "display-firmware"))
    with TestClient(startup_app):
        assert adapters.app_unit.start_calls == ["face"]
        assert adapters.state.read_preempted_app() is None
        interrupted = adapters.state.read_apps_state().apps["resume"].last_job
        assert interrupted is not None
        assert interrupted.error == "interrupted"


def test_wifi_monitor_starts_when_autostart_inspection_fails(
    startup_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(startup_app, "auto", auto=True)
    startup_app.state.settings = replace(startup_app.state.settings, wifi_setup_enabled=True)
    monitor = threading.Event()
    monkeypatch.setattr(startup_app.state.wifi_setup, "run_forever", monitor.set)

    def unavailable(name: str) -> None:
        raise RuntimeError("unit service unavailable")

    monkeypatch.setattr(startup_app.state.adapters.app_unit, "status", unavailable)
    with TestClient(startup_app):
        assert monitor.wait(3)


def test_initial_maintenance_waits_for_non_display_autostart_to_release_locks(
    startup_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from palmimo_portal.core.apps_start_job import StartJobRunner

    install(startup_app, "auto", auto=True, display=False)
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.app_environment.display_firmware_need = "none"
    startup_app.state.settings = replace(startup_app.state.settings, startup_run_in_thread=True)
    startup_app.state.start_job_runner = StartJobRunner(startup_app.state.start_deps, run_in_thread=True)
    entered = threading.Event()
    release = threading.Event()
    ensured = threading.Event()
    original_start = adapters.app_unit.start
    original_ensure = adapters.platform.ensure_display_firmware

    def start(name: str) -> None:
        entered.set()
        assert release.wait(5)
        original_start(name)

    def ensure() -> DisplayFirmwareResult:
        result = original_ensure()
        ensured.set()
        return result

    monkeypatch.setattr(adapters.app_unit, "start", start)
    monkeypatch.setattr(adapters.platform, "ensure_display_firmware", ensure)
    try:
        with TestClient(startup_app):
            assert entered.wait(3)
            startup_app.state.startup_thread.join(timeout=0.2)
            assert startup_app.state.startup_thread.is_alive()
            assert not ensured.is_set()
            release.set()
            assert ensured.wait(3)
            assert adapters.platform.display_firmware_calls == 1
    finally:
        release.set()
        startup_app.state.startup_thread.join(3)


@pytest.mark.parametrize("by", ["display-firmware", "wifi-setup"])
@pytest.mark.parametrize("tool_unavailable", [False, True])
def test_startup_recovers_preempted_app_when_platform_not_ready(
    startup_app: FastAPI, by: str, tool_unavailable: bool
) -> None:
    from palmimo_portal.ports import PlatformCommandError

    install(startup_app, "face")
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.platform.installed = None
    if tool_unavailable:
        adapters.platform.display_error = PlatformCommandError("display_firmware", "tool unavailable")
    adapters.state.write_preempted_app(PreemptedApp("face", by))
    with TestClient(startup_app):
        assert adapters.app_unit.start_calls == ["face"]
        assert adapters.state.read_preempted_app() is None
        assert all(owner != "initial" for _, owner in adapters.platform.desire_display_firmware_calls)


def test_startup_clears_wifi_hold_when_platform_not_ready(startup_app: FastAPI) -> None:
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.platform.installed = None
    adapters.state.write_preempted_app(PreemptedApp(None, "wifi-setup", hold=True))
    with TestClient(startup_app):
        assert adapters.state.read_preempted_app() is None
        assert adapters.platform.desire_display_firmware_calls == []
        assert adapters.platform.display_firmware_calls == 0


@pytest.mark.parametrize("busy_stage", ["firmware", "assets"])
def test_periodic_recovery_retries_busy_preempt_without_ntp(startup_app: FastAPI, busy_stage: str) -> None:
    from palmimo_portal.ports import DisplayFirmwareSelection, DisplaySyncResult

    install(startup_app, "face")
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.clock.synchronized = False
    adapters.platform.display_firmware_selection = DisplayFirmwareSelection(2, "initial")
    pending = PreemptedApp("face", "display-firmware")
    adapters.state.write_preempted_app(pending)
    if busy_stage == "firmware":
        adapters.platform.display_firmware_result = DisplayFirmwareResult("busy")
    else:
        adapters.platform.display_sync_result = DisplaySyncResult("busy", (), (), "in use")
    with TestClient(startup_app):
        assert adapters.app_unit.start_calls == []
        assert adapters.state.read_preempted_app() == pending
        adapters.platform.display_firmware_result = DisplayFirmwareResult("current")
        adapters.platform.display_sync_result = DisplaySyncResult("synced", (), (), None)
        adapters.clock.monotonic_value += 5
        startup_app.state.periodic_scheduler.tick()
        assert adapters.app_unit.start_calls == ["face"]
        assert adapters.state.read_preempted_app() is None
        adapters.clock.monotonic_value += 5
        startup_app.state.periodic_scheduler.tick()
        assert adapters.app_unit.start_calls == ["face"]


@pytest.mark.parametrize("held_lock", ["apps", "run"])
def test_periodic_recovery_skips_contended_locks_and_resumes_after_release(
    startup_app: FastAPI, monkeypatch: pytest.MonkeyPatch, held_lock: str
) -> None:
    from contextlib import AbstractContextManager

    install(startup_app, "face")
    adapters: FakeAdapterBundle = startup_app.state.adapters
    with TestClient(startup_app):
        adapters.state.write_preempted_app(PreemptedApp("face", "display-firmware"))
        run_lock = adapters.state.lock_run
        budgets: list[float] = []

        def observed_run_lock(timeout_s: float = 0) -> AbstractContextManager[None]:
            budgets.append(timeout_s)
            return run_lock(timeout_s=0)

        monkeypatch.setattr(adapters.state, "lock_run", observed_run_lock)
        with adapters.state.lock_apps() if held_lock == "apps" else run_lock():
            adapters.clock.monotonic_value += 5
            startup_app.state.periodic_scheduler.tick()
            assert adapters.app_unit.start_calls == []
            assert adapters.state.read_preempted_app() is not None
            assert budgets == ([] if held_lock == "apps" else [0])
        adapters.clock.monotonic_value += 5
        startup_app.state.periodic_scheduler.tick()
        assert adapters.app_unit.start_calls == ["face"]
        assert adapters.state.read_preempted_app() is None
        assert all(budget == 0 for budget in budgets)


def test_periodic_recovery_cannot_consume_startup_preempt_before_autostart_decision(
    startup_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from palmimo_portal.api import app as app_module
    from palmimo_portal.ports import DisplayFirmwareSelection, UnitStatus

    install(startup_app, "face")
    install(startup_app, "auto", auto=True)
    adapters: FakeAdapterBundle = startup_app.state.adapters
    adapters.platform.display_firmware_selection = DisplayFirmwareSelection(2, "initial")
    adapters.state.write_preempted_app(PreemptedApp("face", "display-firmware"))
    original_restore = app_module.restore_preempted_app
    original_start = adapters.app_unit.start

    def start(name: str) -> None:
        original_start(name)
        if name == "face":
            adapters.app_unit.statuses[name] = UnitStatus("inactive", "dead", "success", 0)

    def restore(deps: object) -> bool:
        adapters.clock.monotonic_value += 5
        startup_app.state.periodic_scheduler.tick()
        return original_restore(startup_app.state.start_deps)

    monkeypatch.setattr(adapters.app_unit, "start", start)
    monkeypatch.setattr(app_module, "restore_preempted_app", restore)
    with TestClient(startup_app):
        assert adapters.app_unit.start_calls == ["face"]
        assert adapters.state.read_preempted_app() is None


def test_periodic_recovery_defers_stopping_unit_without_waiting(
    startup_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from palmimo_portal.core import display_firmware
    from palmimo_portal.ports import UnitStatus

    install(startup_app, "face")
    adapters: FakeAdapterBundle = startup_app.state.adapters
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        adapters.clock.monotonic_value += seconds

    with TestClient(startup_app):
        adapters.state.write_preempted_app(PreemptedApp("face", "display-firmware"))
        adapters.app_unit.statuses["face"] = UnitStatus("deactivating", "stop", "success", 0)
        monkeypatch.setattr(display_firmware, "time", SimpleNamespace(monotonic=adapters.clock.monotonic, sleep=sleep))
        adapters.clock.monotonic_value += 5
        startup_app.state.periodic_scheduler.tick()
        assert waits == []
        assert adapters.state.read_preempted_app() is not None
        assert adapters.app_unit.start_calls == []
        adapters.app_unit.statuses["face"] = UnitStatus("inactive", "dead", "success", 0)
        adapters.clock.monotonic_value += 5
        startup_app.state.periodic_scheduler.tick()
        assert adapters.app_unit.start_calls == ["face"]
        assert adapters.state.read_preempted_app() is None
