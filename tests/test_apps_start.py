"""Behavioral tests for `core/apps_start.py`: the design doc 3.5 start/stop prechecks and status mapping."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import pytest

from palmimo_portal.core.apps_jobs import AppsJobContext
from palmimo_portal.core.apps_start import (
    AppBusyError,
    AppJobInProgressStartError,
    AppRunningError,
    EnvUnboundError,
    ParamsInvalidError,
    ParamsMissingError,
    PlatformNotReadyError,
    StartDeps,
    VenvMissingError,
    derive_run_status,
    precheck_report,
    reconcile_run_dirs,
    resolve_running_url,
    start_app,
    stop_app,
)
from palmimo_portal.core.manifest import manifest_snapshot, parse_manifest
from palmimo_portal.ports import AppNotFoundError, AppRecord, AppSource, AppsState, PolkitDeniedError, UnitStatus
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


_DEFAULT_MANIFEST = """
schema = 1
name = "app"
description = "d"
command = ["run", "--port", "{port}", "--label", "{label}"]
url = "http://{host}:{port}/"

[env.API_KEY]
description = "required key"

[env.OPTIONAL_KEY]
description = "optional key"
required = false

[params.port]
type = "int"
default = 8080

[params.label]
type = "string"
"""


def _write_app(
    apps_dir: Path,
    name: str,
    *,
    manifest: str = _DEFAULT_MANIFEST,
    subdir: str | None = None,
    with_venv: bool = True,
    workspace: bool = False,
) -> Path:
    root = apps_dir / name
    project_dir = root / subdir if subdir else root
    project_dir.mkdir(parents=True)
    (project_dir / "palmimo.toml").write_text(manifest, encoding="utf-8")
    if workspace:
        (root / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['*']\n", encoding="utf-8")
        (project_dir / "pyproject.toml").write_text(f"[project]\nname = '{name}'\n", encoding="utf-8")
    if with_venv:
        venv_bin = project_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("", encoding="utf-8")
    return root


def _record(
    name: str, *, params: dict | None = None, source: AppSource | None = None, manifest: str = _DEFAULT_MANIFEST
) -> AppRecord:
    return AppRecord(
        name=name,
        source=source or AppSource(type="zip"),
        installed_at=0.0,
        params=params if params is not None else {"label": "x"},
        autostart=False,
        last_job=None,
        manifest=manifest_snapshot(parse_manifest(manifest)),
        id=name,
    )


@pytest.fixture
def apps_dir(tmp_path: Path) -> Path:
    path = tmp_path / "apps"
    path.mkdir()
    return path


@pytest.fixture
def ctx(apps_dir: Path, tmp_path: Path) -> AppsJobContext:
    return AppsJobContext(
        apps_dir=apps_dir,
        uv_cache_dir=tmp_path / "uv-cache",
        git=FakeGitPort(),
        uv=FakeUvPort(),
        sync_unit=FakeSyncUnitPort(),
        journal=FakeJournalPort(),
        disk=FakeDiskPort(),
        secrets=FakeSecretsStore(),
    )


@pytest.fixture
def deps(ctx: AppsJobContext) -> StartDeps:
    return StartDeps(state=FakeStateStore(), apps_job_ctx=ctx, app_unit=FakeAppUnitPort(), run_dir=FakeRunDirPort())


def _app_unit(deps: StartDeps) -> FakeAppUnitPort:
    """Access the fake concretely: `StartDeps.app_unit` is typed to the port protocol."""
    return cast(FakeAppUnitPort, deps.app_unit)


def _run_dir(deps: StartDeps) -> FakeRunDirPort:
    return cast(FakeRunDirPort, deps.run_dir)


def _state(deps: StartDeps) -> FakeStateStore:
    return cast(FakeStateStore, deps.state)


def _install(deps: StartDeps, apps_dir: Path, name: str, *, bind_required: bool = True, **kwargs: object) -> AppRecord:
    _write_app(apps_dir, name, **kwargs)  # type: ignore[arg-type]
    manifest = kwargs.get("manifest", _DEFAULT_MANIFEST)
    assert isinstance(manifest, str)
    record = _record(name, manifest=manifest)
    existing = _state(deps).read_apps_state().apps
    _state(deps).write_apps_state(AppsState(apps={**existing, name: record}))
    if bind_required:
        deps.apps_job_ctx.secrets.set_secret("MY_KEY", "sekrit")
        deps.apps_job_ctx.secrets.write_bindings(name, {"API_KEY": "MY_KEY"})
    return record


def test_start_app_raises_app_not_found_when_app_is_not_installed(deps: StartDeps) -> None:
    with pytest.raises(AppNotFoundError):
        start_app(deps, "missing", host="host")


def test_start_app_raises_app_busy_when_run_lock_is_already_held(deps: StartDeps, apps_dir: Path) -> None:
    # Without this, two concurrent starts (or a start racing autostart) could both
    # observe "nothing running" and both proceed -- the whole point of run.lock.
    _install(deps, apps_dir, "app")
    with _state(deps).lock_run(), pytest.raises(AppBusyError):
        start_app(deps, "app", host="host")


@pytest.mark.parametrize("active_state", ["active", "activating", "deactivating"])
def test_start_app_raises_app_running_when_another_app_occupies_the_slot(
    deps: StartDeps, apps_dir: Path, active_state: str
) -> None:
    # Deactivating still holds the servo bus (design doc 3.5 step 1) -- must refuse, not just "active".
    _install(deps, apps_dir, "app")
    _install(deps, apps_dir, "other")
    _app_unit(deps).simulate_active_state(
        "other", UnitStatus(active_state=active_state, sub_state="x", result="success", exec_main_status=0)
    )

    with pytest.raises(AppRunningError) as excinfo:
        start_app(deps, "app", host="host")

    assert excinfo.value.running == "other"


def test_start_app_raises_app_job_in_progress_when_name_is_its_own_current_job(deps: StartDeps, apps_dir: Path) -> None:
    # An install/update/delete job's own prechecks ran before the job started -- this closes
    # the window where a start slips in afterward, while the job is still mid-swap.
    from palmimo_portal.ports import AppJob

    record = _install(deps, apps_dir, "app")
    state = _state(deps)
    job = AppJob(id="j1", kind="update", state="running", step="sync", error=None, started_at=0.0, finished_at=None)
    state.write_apps_state(AppsState(apps={"app": record}, current_job=job, current_job_app="app"))

    with pytest.raises(AppJobInProgressStartError):
        start_app(deps, "app", host="host")


def test_start_app_raises_platform_not_ready_when_the_gate_refuses(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    deps = StartDeps(
        state=_state(deps),
        apps_job_ctx=deps.apps_job_ctx,
        app_unit=_app_unit(deps),
        run_dir=_run_dir(deps),
        platform_ready=lambda: False,
    )

    with pytest.raises(PlatformNotReadyError):
        start_app(deps, "app", host="host")


def test_start_app_uses_snapshot_but_rejects_a_missing_app_tree(deps: StartDeps, apps_dir: Path) -> None:
    # The snapshot remains authoritative for configuration, but the app still
    # needs its installed venv to exist before it can be run.
    _state(deps).write_apps_state(AppsState(apps={"app": _record("app")}))
    deps.apps_job_ctx.secrets.set_secret("MY_KEY", "sekrit")
    deps.apps_job_ctx.secrets.write_bindings("app", {"API_KEY": "MY_KEY"})

    with pytest.raises(VenvMissingError):
        start_app(deps, "app", host="host")


def test_start_app_raises_env_unbound_when_a_required_env_var_has_no_binding(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app", bind_required=False)

    with pytest.raises(EnvUnboundError) as excinfo:
        start_app(deps, "app", host="host")

    assert excinfo.value.names == ["API_KEY"]


def test_start_app_raises_env_unbound_when_the_bound_secret_was_deleted(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    deps.apps_job_ctx.secrets.write_bindings("app", {})  # binding dropped, secret still registered elsewhere

    with pytest.raises(EnvUnboundError) as excinfo:
        start_app(deps, "app", host="host")

    assert excinfo.value.names == ["API_KEY"]


def test_start_app_raises_params_missing_when_a_required_param_has_no_value(deps: StartDeps, apps_dir: Path) -> None:
    _write_app(apps_dir, "app")
    _state(deps).write_apps_state(AppsState(apps={"app": _record("app", params={})}))
    deps.apps_job_ctx.secrets.set_secret("MY_KEY", "sekrit")
    deps.apps_job_ctx.secrets.write_bindings("app", {"API_KEY": "MY_KEY"})

    with pytest.raises(ParamsMissingError) as excinfo:
        start_app(deps, "app", host="host")

    assert excinfo.value.names == ["label"]


def test_start_app_raises_params_invalid_when_a_saved_value_no_longer_fits_the_manifest(
    deps: StartDeps, apps_dir: Path
) -> None:
    # port's saved value was valid when set; the manifest changed underneath it (design doc 3.5 step 4).
    manifest = _DEFAULT_MANIFEST.replace('type = "int"\ndefault = 8080', 'type = "int"\nmin = 9000')
    _write_app(apps_dir, "app", manifest=manifest)
    _state(deps).write_apps_state(
        AppsState(apps={"app": _record("app", params={"port": 8080, "label": "x"}, manifest=manifest)})
    )
    deps.apps_job_ctx.secrets.set_secret("MY_KEY", "sekrit")
    deps.apps_job_ctx.secrets.write_bindings("app", {"API_KEY": "MY_KEY"})

    with pytest.raises(ParamsInvalidError) as excinfo:
        start_app(deps, "app", host="host")

    assert excinfo.value.names == ["port"]


def test_start_app_raises_venv_missing_when_the_venv_does_not_exist(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app", with_venv=False)

    with pytest.raises(VenvMissingError):
        start_app(deps, "app", host="host")


def test_start_app_removes_run_dir_when_set_device_allow_raises(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    _app_unit(deps).raise_on_set_device_allow = RuntimeError("polkit gone")

    with pytest.raises(RuntimeError):
        start_app(deps, "app", host="host")

    assert "app" not in _run_dir(deps).written


def test_start_app_removes_run_dir_when_start_raises(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    _app_unit(deps).raise_on_start = RuntimeError("dbus timeout")

    with pytest.raises(RuntimeError):
        start_app(deps, "app", host="host")

    assert "app" not in _run_dir(deps).written


def test_start_app_writes_exactly_the_bound_env_keys_and_nothing_else(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    # OPTIONAL_KEY is declared but never bound -- must be absent from the written env, not empty-valued.
    start_app(deps, "app", host="host")

    assert _run_dir(deps).written["app"]["env"] == {"API_KEY": "sekrit"}


def test_start_app_includes_a_bound_optional_env_var(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    deps.apps_job_ctx.secrets.set_secret("OPT_STORE", "opt-value")
    deps.apps_job_ctx.secrets.write_bindings("app", {"API_KEY": "MY_KEY", "OPTIONAL_KEY": "OPT_STORE"})

    start_app(deps, "app", host="host")

    assert _run_dir(deps).written["app"]["env"] == {"API_KEY": "sekrit", "OPTIONAL_KEY": "opt-value"}


def test_start_app_never_logs_a_bound_secret_value(
    deps: StartDeps, apps_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="palmimo_portal")
    _install(deps, apps_dir, "app")

    start_app(deps, "app", host="host")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "sekrit" not in log_text


def test_start_app_argv_json_for_a_plain_app_has_matching_cwd_and_project(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")

    start_app(deps, "app", host="host")

    written = _run_dir(deps).written["app"]
    expected = str(apps_dir / "app")
    assert written["cwd"] == expected
    assert written["project"] == expected


@pytest.mark.parametrize("workspace", [False, True])
def test_start_app_argv_json_for_a_subdir_app_has_matching_cwd_and_project(
    deps: StartDeps, apps_dir: Path, workspace: bool
) -> None:
    # Whether `apps_dir/app` (the clone root) happens to be a uv workspace listing
    # `examples/app` as a member makes no difference here -- membership is uv's
    # decision at sync time, not this precheck's.
    source = AppSource(type="git", url="https://example/repo", ref="v1", ref_kind="tag", subdir="examples/app")
    _write_app(apps_dir, "app", subdir="examples/app", workspace=workspace)
    _state(deps).write_apps_state(AppsState(apps={"app": _record("app", source=source)}))
    deps.apps_job_ctx.secrets.set_secret("MY_KEY", "sekrit")
    deps.apps_job_ctx.secrets.write_bindings("app", {"API_KEY": "MY_KEY"})

    start_app(deps, "app", host="host")

    written = _run_dir(deps).written["app"]
    expected = str(apps_dir / "app" / "examples" / "app")
    assert written["cwd"] == expected
    assert written["project"] == expected


def test_start_app_argv_substitutes_params_and_host(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")

    start_app(deps, "app", host="my-host")

    assert _run_dir(deps).written["app"]["argv"] == ["run", "--port", "8080", "--label", "x"]


def test_start_app_sets_device_allow_camera_expands_to_three_groups(deps: StartDeps, apps_dir: Path) -> None:
    manifest = _DEFAULT_MANIFEST.replace('name = "app"', 'name = "app"\ndevices = ["camera"]')
    _install(deps, apps_dir, "app", manifest=manifest)

    start_app(deps, "app", host="host")

    [(name, specs)] = _app_unit(deps).device_allow_calls
    assert name == "app"
    assert specs == ["char-video4linux", "char-media", "char-dma_heap"]


def test_start_app_uses_the_installed_manifest_snapshot_for_device_allow(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    (apps_dir / "app" / "palmimo.toml").write_text(
        _DEFAULT_MANIFEST.replace('name = "app"', 'name = "app"\ndevices = ["camera"]'), encoding="utf-8"
    )

    start_app(deps, "app", host="host")

    assert _app_unit(deps).device_allow_calls == [("app", [])]


def test_start_app_calls_start_after_writing_run_dir_and_setting_devices(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")

    start_app(deps, "app", host="host")

    assert _app_unit(deps).start_calls == ["app"]
    assert "app" in _run_dir(deps).written


def test_stop_app_stops_the_unit_and_removes_the_run_dir(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    start_app(deps, "app", host="host")

    stop_app(deps, "app")

    assert _app_unit(deps).stop_calls == ["app"]
    assert "app" not in _run_dir(deps).written


def test_reconcile_run_dirs_removes_a_run_dir_left_over_for_a_stopped_unit(deps: StartDeps, apps_dir: Path) -> None:
    record_running = _install(deps, apps_dir, "running-app")
    record_stopped = _install(deps, apps_dir, "stopped-app")
    _run_dir(deps).write(
        "running-app", env={}, argv=["run"], cwd=str(apps_dir / "running-app"), project=str(apps_dir / "running-app")
    )
    _run_dir(deps).write(
        "stopped-app", env={}, argv=["run"], cwd=str(apps_dir / "stopped-app"), project=str(apps_dir / "stopped-app")
    )
    _app_unit(deps).simulate_active_state(
        "running-app", UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )

    reconcile_run_dirs(
        deps.app_unit, deps.run_dir, AppsState(apps={"running-app": record_running, "stopped-app": record_stopped})
    )

    assert "running-app" in _run_dir(deps).written
    assert "stopped-app" not in _run_dir(deps).written


def test_stop_app_raises_app_not_found_when_app_is_not_installed(deps: StartDeps) -> None:
    with pytest.raises(AppNotFoundError):
        stop_app(deps, "missing")


def test_stop_app_raises_app_busy_when_run_lock_is_already_held(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    with _state(deps).lock_run(), pytest.raises(AppBusyError):
        stop_app(deps, "app")


def test_stop_app_removes_the_run_dir_even_when_stop_unit_raises(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    start_app(deps, "app", host="host")
    _app_unit(deps).raise_on_stop = PolkitDeniedError("palmimo-app@app.service", "stop")

    with pytest.raises(PolkitDeniedError):
        stop_app(deps, "app")

    assert "app" not in _run_dir(deps).written


@pytest.mark.parametrize(
    ("unit_status", "expected_status", "expected_exit_code", "expected_reason"),
    [
        (
            UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0),
            "running",
            None,
            None,
        ),
        (
            UnitStatus(active_state="activating", sub_state="start", result="success", exec_main_status=0),
            "starting",
            None,
            None,
        ),
        (
            UnitStatus(active_state="deactivating", sub_state="stop", result="success", exec_main_status=0),
            "stopping",
            None,
            None,
        ),
        (
            UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0),
            "stopped",
            None,
            None,
        ),
        (
            UnitStatus(active_state="failed", sub_state="failed", result="exit-code", exec_main_status=78),
            "needs_repair",
            None,
            None,
        ),
        (
            UnitStatus(active_state="failed", sub_state="failed", result="exit-code", exec_main_status=1),
            "failed",
            1,
            None,
        ),
        (
            UnitStatus(active_state="failed", sub_state="failed", result="signal", exec_main_status=0),
            "failed",
            None,
            "signal",
        ),
        (
            UnitStatus(active_state="failed", sub_state="failed", result="timeout", exec_main_status=0),
            "failed",
            None,
            "timeout",
        ),
        (
            UnitStatus(active_state="failed", sub_state="failed", result="start-limit-hit", exec_main_status=0),
            "failed",
            None,
            "start-limit-hit",
        ),
        (
            UnitStatus(active_state="failed", sub_state="failed", result="resources", exec_main_status=0),
            "failed",
            None,
            "env file",
        ),
    ],
)
def test_derive_run_status_maps_systemd_result_to_the_design_doc_table(
    unit_status: UnitStatus, expected_status: str, expected_exit_code: int | None, expected_reason: str | None
) -> None:
    result = derive_run_status(unit_status)

    assert result.status == expected_status
    assert result.exit_code == expected_exit_code
    assert result.reason == expected_reason


def test_resolve_running_url_substitutes_params_and_host(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    record = _state(deps).read_apps_state().apps["app"]

    url = resolve_running_url(deps, record, host="my-host")

    assert url == "http://my-host:8080/"


# `precheck_report` must report exactly the code `start_app` would have refused with, without
# actually starting anything -- design doc 3.2's diagnostics `[precheck]` section relies on this.


def test_precheck_report_is_ok_when_start_would_succeed(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")

    result = precheck_report(deps, "app", host="host")

    assert result == precheck_report(deps, "app", host="host")
    assert result.ok is True
    assert result.code is None


def test_precheck_report_reports_app_not_found_for_an_uninstalled_app(deps: StartDeps) -> None:
    result = precheck_report(deps, "missing", host="host")

    assert result.ok is False
    assert result.code == "app_not_found"


def test_precheck_report_reports_env_unbound_with_names(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app", bind_required=False)

    result = precheck_report(deps, "app", host="host")

    assert result.ok is False
    assert result.code == "env_unbound"
    assert result.names == ["API_KEY"]


def test_precheck_report_reports_app_running_with_the_running_app(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")
    _install(deps, apps_dir, "other")
    _app_unit(deps).simulate_active_state(
        "other", UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )

    result = precheck_report(deps, "app", host="host")

    assert result.ok is False
    assert result.code == "app_running"
    assert result.running == "other"


def test_precheck_report_does_not_start_the_app_or_write_a_run_dir(deps: StartDeps, apps_dir: Path) -> None:
    _install(deps, apps_dir, "app")

    precheck_report(deps, "app", host="host")

    assert _app_unit(deps).start_calls == []
    assert _run_dir(deps).written == {}
