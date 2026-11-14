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


def test_startup_serves_http_while_display_ensure_is_blocked(
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
