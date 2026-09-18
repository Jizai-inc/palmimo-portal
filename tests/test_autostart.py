"""Tests for startup autostart (design doc 2.5): the `_lifespan` step in `api/app.py` after finalize."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api.app import create_app
from palmimo_portal.ports import AppRecord, AppSource, AppsState, PlatformVerifyDiff, UnitStatus
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        allowed_hosts=frozenset({"testserver"}),
        static_dir=tmp_path / "static-not-built",
        apps_dir=tmp_path / "apps",
        uv_cache_dir=tmp_path / "uv-cache",
        **overrides,  # type: ignore[arg-type]
    )


def _record(name: str, *, autostart: bool) -> AppRecord:
    return AppRecord(
        name=name, source=AppSource(type="zip"), installed_at=0.0, params={}, autostart=autostart, last_job=None
    )


_MANIFEST = """
schema = 1
name = "{name}"
description = "d"
command = ["run"]

[env.API_KEY]
description = "required key"
"""


def _write_app(settings: Settings, name: str) -> None:
    # A manifest + venv present on disk (so startup finalize does not mark this app `broken`
    # and exclude it from autostart selection before _run_autostart ever sees it), but no
    # bound secret for its required env var -- start_app fails at that precheck.
    project_dir = settings.apps_dir / name
    project_dir.mkdir(parents=True)
    (project_dir / "palmimo.toml").write_text(_MANIFEST.format(name=name), encoding="utf-8")
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("", encoding="utf-8")


def _messages(caplog: pytest.LogCaptureFixture, substring: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if substring in record.getMessage()]


def test_autostart_selects_no_app_when_none_are_flagged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(_settings(tmp_path))
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app):
        pass

    assert _messages(caplog, "autostart: no app selected")


def test_autostart_starts_at_most_one_app_and_logs_the_rest_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)
    _write_app(settings, "app-a")
    _write_app(settings, "app-b")
    app = create_app(settings)
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.state.write_apps_state(
        AppsState(apps={"app-a": _record("app-a", autostart=True), "app-b": _record("app-b", autostart=True)})
    )
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app):
        pass

    assert _messages(caplog, "autostart: selected name=app-a") == ["autostart: selected name=app-a"]
    assert _messages(caplog, "autostart: skipped name=app-b") == ["autostart: skipped name=app-b"]
    assert not _messages(caplog, "autostart: skipped name=app-a")


def test_autostart_never_selects_an_app_that_is_not_flagged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(_settings(tmp_path))
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.state.write_apps_state(AppsState(apps={"app-a": _record("app-a", autostart=False)}))
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app):
        pass

    assert _messages(caplog, "autostart: no app selected")


def test_startup_stops_an_orphan_sync_unit_before_cleaning_staging_and_trash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A process that died mid-job can leave its palmimo-app-sync@ unit running against the
    # `.staging/<id>/` tree cleanup is about to purge -- it must be stopped first.
    from palmimo_portal.core.apps_jobs import AppsJobContext
    from palmimo_portal.testing.fakes import FakeSyncUnitPort

    app = create_app(_settings(tmp_path))
    ctx: AppsJobContext = app.state.apps_job_context
    sync_unit = ctx.sync_unit
    assert isinstance(sync_unit, FakeSyncUnitPort)
    sync_unit.active_instances.add("jstuck")
    caplog.set_level(logging.WARNING, logger="palmimo_portal")

    with TestClient(app):
        pass

    assert "jstuck" in sync_unit.stop_calls
    assert _messages(caplog, "apps: stopped orphan sync unit instance=jstuck")


def test_autostart_runs_after_startup_finalize(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(_settings(tmp_path))
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app):
        pass

    finalize_index = next(
        i for i, record in enumerate(caplog.records) if "apps: startup finalize" in record.getMessage()
    )
    autostart_index = next(i for i, record in enumerate(caplog.records) if "autostart:" in record.getMessage())
    assert finalize_index < autostart_index


def test_autostart_failure_does_not_prevent_the_app_from_starting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # app-a's required env var has no bound secret -- start_app fails at that precheck.
    # The point of this test: that failure must not propagate out of the lifespan handler.
    settings = _settings(tmp_path)
    _write_app(settings, "app-a")
    app = create_app(settings)
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.state.write_apps_state(AppsState(apps={"app-a": _record("app-a", autostart=True)}))
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app) as client:
        response = client.get("/api/v1/system/status")

    assert response.status_code == 200
    assert _messages(caplog, "autostart: failed reason=env_unbound")


def test_autostart_disabled_by_settings_never_attempts_a_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(_settings(tmp_path, autostart_enabled=False))
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.state.write_apps_state(AppsState(apps={"app-a": _record("app-a", autostart=True)}))
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app):
        pass

    assert not _messages(caplog, "autostart:")
    assert adapters.app_unit.start_calls == []


def test_autostart_skips_an_app_whose_unit_is_already_active(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # The unit can already be running on its own systemd lifecycle (surviving a Portal
    # restart) -- starting it again would reset DeviceAllow out from under it.
    settings = _settings(tmp_path)
    _write_app(settings, "app-a")
    app = create_app(settings)
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.state.write_apps_state(AppsState(apps={"app-a": _record("app-a", autostart=True)}))
    adapters.app_unit.statuses["app-a"] = UnitStatus(
        active_state="active", sub_state="running", result="success", exec_main_status=0
    )
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app):
        pass

    assert _messages(caplog, "autostart: already running name=app-a")
    assert adapters.app_unit.device_allow_calls == []
    assert adapters.app_unit.start_calls == []


def test_startup_verify_logs_begin_and_end(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(_settings(tmp_path))
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app):
        pass

    assert _messages(caplog, "app-platform: startup verify begin")
    assert _messages(caplog, "app-platform: startup verify end")


def test_startup_readiness_refresh_failure_does_not_prevent_the_app_from_serving(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(_settings(tmp_path))
    adapters: FakeAdapterBundle = app.state.adapters

    def _boom() -> None:
        raise RuntimeError("verify crashed")

    adapters.platform.read_installed = _boom  # type: ignore[method-assign]
    caplog.set_level(logging.INFO, logger="palmimo_portal")

    with TestClient(app) as client:
        response = client.get("/api/v1/system/status")

    assert response.status_code == 200
    assert _messages(caplog, "app-platform: startup verify failed")


def test_startup_not_ready_log_includes_missing_paths_and_journal_readable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    platform_dir = tmp_path / "platform"
    bundle_dir = platform_dir / "current"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    app = create_app(_settings(tmp_path, platform_dir=platform_dir))
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.platform.verify_diffs = [PlatformVerifyDiff(path="/usr/lib/palmimo/app-launch", kind="missing")]
    adapters.journal.readable = False
    caplog.set_level(logging.WARNING, logger="palmimo_portal")

    with TestClient(app):
        pass

    [message] = _messages(caplog, "app-platform: not ready")
    assert "diffs=[('missing', '/usr/lib/palmimo/app-launch')]" in message
    assert "journal_readable=False" in message


def test_create_app_wires_a_working_start_deps(tmp_path: Path) -> None:
    # Sanity: app.state.start_deps exists and start/stop endpoints (api/apps.py) can use it --
    # without this wiring, every request to those routes 500s with an AttributeError.
    app: FastAPI = create_app(_settings(tmp_path))

    assert app.state.start_deps is not None


def test_startup_keeps_app_directories_when_the_ledger_is_corrupt(tmp_path: Path) -> None:
    # A corrupt apps.json reads as an empty ledger; the orphan sweep must not take that
    # as "no apps are installed" and trash every installed app.
    from palmimo_portal.core.apps_jobs import AppsJobContext
    from palmimo_portal.testing.fakes import FakeStateStore

    app = create_app(_settings(tmp_path))
    ctx: AppsJobContext = app.state.apps_job_context
    installed = ctx.apps_dir / "palmimo-teleop"
    installed.mkdir(parents=True)
    (installed / "palmimo.toml").write_text("schema = 1")
    state_store = app.state.adapters.state
    assert isinstance(state_store, FakeStateStore)
    state_store.apps_state_corrupt = True

    with TestClient(app):
        pass

    assert (installed / "palmimo.toml").is_file()
