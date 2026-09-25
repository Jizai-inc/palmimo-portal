"""Precheck and orchestration for ``POST /apps/{id}/start`` and ``/stop`` (design doc 3.5).

:func:`start_app` runs the numbered prechecks in order and stops at the
first failure -- nothing is changed until step 6 (write the run dir), and
any failure from there on removes it again (never leaves a secret-bearing
``env`` file behind). Every failure raises a subclass of
:class:`AppStartError` carrying the i18n ``code`` and any structured
params ``api/apps.py`` needs for the response body, so that module holds
no separate code-mapping table of its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from palmimo_portal.core.apps_jobs import AppsJobContext, read_manifest_for_app
from palmimo_portal.core.apps_layout import LayoutPaths, resolve_layout
from palmimo_portal.core.manifest import (
    Manifest,
    ManifestValidationError,
    resolve_command,
    resolve_url,
    validate_param_values,
)
from palmimo_portal.ports import (
    AppNotFoundError,
    AppRecord,
    AppsState,
    AppUnitPort,
    InvalidManifestSourceError,
    RunDirPort,
    RunLockTimeoutError,
    StateStore,
    UnitStatus,
)


logger = logging.getLogger("palmimo_portal")

APP_UNIT_TEMPLATE = "palmimo-app@{name}.service"

#: design doc 2.4 -- one declared `[devices]` entry expands to these `DeviceAllow` class specs.
DEVICE_ALLOW_SPECS: dict[str, tuple[str, ...]] = {
    "camera": ("char-video4linux", "char-media", "char-dma_heap"),
    "audio": ("char-alsa",),
    "motor_display": ("char-ttyACM",),
}

#: `UnitStatus.active_state` values meaning "occupying the one-app-at-a-time slot" (design doc 3.5 step 1).
#: Also what `api/app.py`'s autostart uses to detect a unit already running on a live cgroup --
#: resetting `DeviceAllow` on one (`set_device_allow` always clears first) would revoke device
#: access out from under an already-running process.
RUNNING_ACTIVE_STATES = frozenset({"active", "activating", "deactivating"})


def app_unit_name(name: str) -> str:
    return APP_UNIT_TEMPLATE.format(name=name)


class AppStartError(Exception):
    """Base for every ``start_app``/``stop_app`` failure. ``code`` is the i18n key ``api/apps.py`` returns."""

    def __init__(self, code: str, **params: Any) -> None:
        self.code = code
        self.params = params
        super().__init__(code)


class AppBusyError(AppStartError):
    def __init__(self) -> None:
        super().__init__("app_busy")


class AppJobInProgressStartError(AppStartError):
    """Raised when any install/update/delete job is in flight, against any app.

    Distinct from :class:`~palmimo_portal.ports.AppsLockTimeoutError` (design
    doc 3.5): ``run.lock`` and ``apps.lock`` are independent axes, so a start
    request can acquire ``run.lock`` freely while an unrelated app's job is
    running -- this checks the *ledger*'s ``current_job_app`` instead. A
    job's sync step runs untrusted build hooks as the same uid an app unit
    runs as (design doc 3.10), so this must refuse every app's start, not
    just the job's own target.
    """

    def __init__(self) -> None:
        super().__init__("app_job_in_progress")


class AppRunningError(AppStartError):
    def __init__(self, running: str) -> None:
        super().__init__("app_running", running=running)
        self.running = running


class ManifestInvalidError(AppStartError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("manifest_invalid", errors=errors)


class EnvUnboundError(AppStartError):
    def __init__(self, names: list[str]) -> None:
        super().__init__("env_unbound", names=names)
        self.names = names


class ParamsMissingError(AppStartError):
    def __init__(self, names: list[str]) -> None:
        super().__init__("params_missing", names=names)
        self.names = names


class ParamsInvalidError(AppStartError):
    def __init__(self, names: list[str]) -> None:
        super().__init__("params_invalid", names=names)
        self.names = names


class VenvMissingError(AppStartError):
    def __init__(self) -> None:
        super().__init__("venv_missing")


class PlatformNotReadyError(AppStartError):
    def __init__(self) -> None:
        super().__init__("platform_not_ready")


@dataclass(frozen=True)
class StartDeps:
    """Every port :func:`start_app`/:func:`stop_app` need, gathered in one place."""

    state: StateStore
    apps_job_ctx: AppsJobContext
    app_unit: AppUnitPort
    run_dir: RunDirPort
    #: Injected readiness gate (design doc 3.6's "new Portal on an old image" case) -- always
    #: True except where a platform-bundle feature wires a real check in.
    platform_ready: Callable[[], bool] = lambda: True
    now: Callable[[], float] = time.time


def _layout_for(ctx: AppsJobContext, record: AppRecord) -> LayoutPaths:
    return resolve_layout(ctx.app_dir(record.id), record.source.subdir)


def _other_active_app(deps: StartDeps, name: str, apps_state: AppsState) -> str | None:
    for other_name in apps_state.apps:
        if other_name == name:
            continue
        status = deps.app_unit.status(other_name)
        if status.active_state in RUNNING_ACTIVE_STATES:
            return other_name
    return None


def start_app(deps: StartDeps, name: str, *, host: str) -> None:
    """Run every design-doc-3.5 precheck for ``name`` and start it, in order, changing nothing on failure.

    Raises:
        AppNotFoundError: no app named ``name`` is installed.
        AppStartError: one of steps 0-9 refused (see the module docstring
            for the subclass-per-code shape).
    """
    # A `@contextlib.contextmanager`-decorated `lock_run()` only actually attempts the
    # acquire on `__enter__()`, not on the call that builds the context manager -- entered
    # by hand here (rather than a `with` statement) so `RunLockTimeoutError` from a failed
    # acquire is caught, instead of only ever seeing an acquire that succeeds.
    lock_cm = deps.state.lock_run()
    try:
        lock_cm.__enter__()
    except RunLockTimeoutError:
        raise AppBusyError() from None
    try:
        _start_app_locked(deps, name, host=host)
    finally:
        lock_cm.__exit__(None, None, None)


@dataclass(frozen=True)
class _StartPlan:
    """Everything :func:`_start_app_locked` needs once every precheck (steps 1-5) has passed."""

    record: AppRecord
    manifest: Manifest
    bindings: dict[str, str]
    env: dict[str, str]
    argv: list[str]
    layout: LayoutPaths
    devices: list[str]


def _run_prechecks(deps: StartDeps, name: str, *, host: str) -> _StartPlan:
    """Run design doc 3.5 steps 1-5, raising the same subclass :func:`start_app` would. Never writes anything."""
    apps_state = deps.state.read_apps_state()
    if name not in apps_state.apps:
        raise AppNotFoundError(name)

    if apps_state.current_job_app is not None:
        raise AppJobInProgressStartError()

    running = _other_active_app(deps, name, apps_state)
    if running is not None:
        raise AppRunningError(running)

    if not deps.platform_ready():
        raise PlatformNotReadyError()

    record = apps_state.apps[name]
    try:
        manifest = read_manifest_for_app(deps.apps_job_ctx, record)
    except InvalidManifestSourceError as error:
        raise ManifestInvalidError([str(error)]) from error
    except ManifestValidationError as error:
        raise ManifestInvalidError(error.errors) from error

    secrets = deps.apps_job_ctx.secrets
    bindings = secrets.read_bindings(name)

    def _bound_value(request_name: str) -> str | None:
        store_name = bindings.get(request_name)
        return secrets.get_secret_value(store_name) if store_name is not None else None

    unbound = sorted(
        request_name
        for request_name, spec in manifest.env.items()
        if spec.required and _bound_value(request_name) is None
    )
    if unbound:
        raise EnvUnboundError(unbound)

    missing_params = sorted(
        param_name
        for param_name, spec in manifest.params.items()
        if not spec.has_default and param_name not in record.params
    )
    if missing_params:
        raise ParamsMissingError(missing_params)

    invalid_params = []
    for param_name, value in record.params.items():
        try:
            validate_param_values(manifest, {param_name: value})
        except ManifestValidationError:
            invalid_params.append(param_name)
    if invalid_params:
        raise ParamsInvalidError(sorted(invalid_params))

    try:
        layout = _layout_for(deps.apps_job_ctx, record)
    except InvalidManifestSourceError as error:
        raise ManifestInvalidError([str(error)]) from error
    if not layout.venv_python.exists():
        raise VenvMissingError()

    argv = resolve_command(manifest, record.params, host=host, app_dir=str(layout.cwd))
    env: dict[str, str] = {}
    for request_name in manifest.env:
        value = _bound_value(request_name)
        if value is not None:
            env[request_name] = value

    devices = sorted(manifest.devices)
    return _StartPlan(
        record=record,
        manifest=manifest,
        bindings=bindings,
        env=env,
        argv=argv,
        layout=layout,
        devices=devices,
    )


@dataclass(frozen=True)
class PrecheckResult:
    """The would-be outcome of :func:`start_app`'s prechecks, without starting anything.

    Backs ``GET /apps/{id}/diagnostics``'s ``[precheck]`` section (design
    doc 3.2): a support agent needs to see *why* a start would fail without
    actually starting the app. ``code``/``names``/``running`` mirror
    :class:`AppStartError`'s own shape 1:1.
    """

    ok: bool
    code: str | None = None
    names: list[str] | None = None
    running: str | None = None


def precheck_report(deps: StartDeps, name: str, *, host: str) -> PrecheckResult:
    """Run every design doc 3.5 precheck (steps 1-5) for ``name`` and report the result, changing nothing.

    Unlike :func:`start_app`, does not take ``run.lock`` -- this is a
    read-only diagnostic snapshot, not a request to actually start the app,
    so it does not need to serialize against a concurrent start/stop.
    """
    try:
        _run_prechecks(deps, name, host=host)
    except AppNotFoundError:
        return PrecheckResult(ok=False, code="app_not_found")
    except AppStartError as error:
        return PrecheckResult(
            ok=False,
            code=error.code,
            names=cast(list[str] | None, error.params.get("names")),
            running=cast(str | None, error.params.get("running")),
        )
    return PrecheckResult(ok=True)


def _start_app_locked(deps: StartDeps, name: str, *, host: str) -> None:
    plan = _run_prechecks(deps, name, host=host)
    device_specs = [spec for device in plan.devices for spec in DEVICE_ALLOW_SPECS[device]]

    deps.run_dir.write(
        name,
        env=plan.env,
        argv=plan.argv,
        cwd=str(plan.layout.cwd),
        project=str(plan.layout.project),
        python=plan.record.requires_python,
    )
    try:
        deps.app_unit.set_device_allow(name, device_specs)
        logger.info(
            "app start requested name=%s argv=%s params=%s bindings=%s devices=%s",
            name,
            plan.argv,
            plan.record.params,
            plan.bindings,
            plan.devices,
        )
        deps.app_unit.start(name)
    except BaseException:
        deps.run_dir.remove(name)
        raise


def stop_app(deps: StartDeps, name: str) -> None:
    """Stop ``name``'s unit and remove its run directory, under ``run.lock``.

    The run directory is removed even if ``StopUnit`` itself raises (a
    polkit refusal, a D-Bus outage) -- it carries the app's secrets-bearing
    ``env`` file, so it must never outlive an attempt to stop the app that
    wrote it.

    Raises:
        AppNotFoundError: no app named ``name`` is installed.
        AppBusyError: another start/stop/autostart already holds ``run.lock``.
    """
    lock_cm = deps.state.lock_run()
    try:
        lock_cm.__enter__()
    except RunLockTimeoutError:
        raise AppBusyError() from None
    try:
        apps_state = deps.state.read_apps_state()
        if name not in apps_state.apps:
            raise AppNotFoundError(name)
        logger.info("app stop requested name=%s", name)
        started = deps.now()
        try:
            deps.app_unit.stop(name)
        finally:
            deps.run_dir.remove(name)
        status = deps.app_unit.status(name)
        logger.info(
            "app stopped name=%s result=%s exec_main_status=%s stop_duration_s=%.3f",
            name,
            status.result,
            status.exec_main_status,
            deps.now() - started,
        )
    finally:
        lock_cm.__exit__(None, None, None)


def reconcile_run_dirs(app_unit: AppUnitPort, run_dir: RunDirPort, state: AppsState) -> None:
    """Remove every ledgered app's run directory whose unit is not active/activating/deactivating.

    Called once at Portal startup, after ledger finalize: a run directory
    left by a process that died before its unit ever started (or after the
    unit already stopped) carries a secrets-bearing ``env`` file that must
    not linger past the process that would have cleaned it up.
    """
    for name in state.apps:
        if app_unit.status(name).active_state not in RUNNING_ACTIVE_STATES:
            run_dir.remove(name)


@dataclass(frozen=True)
class AppRunStatus:
    """The user-facing run status derived from :class:`UnitStatus` (design doc 3.5's status table)."""

    status: str
    exit_code: int | None = None
    reason: str | None = None


def derive_run_status(unit_status: UnitStatus) -> AppRunStatus:
    """Map systemd's `ActiveState`/`Result`/`ExecMainStatus` onto the design doc 3.5 status table."""
    if unit_status.active_state == "active":
        return AppRunStatus("running")
    if unit_status.active_state == "activating":
        return AppRunStatus("starting")
    if unit_status.active_state == "deactivating":
        return AppRunStatus("stopping")
    if unit_status.active_state == "failed":
        if unit_status.result == "exit-code":
            if unit_status.exec_main_status == 78:
                return AppRunStatus("needs_repair")
            return AppRunStatus("failed", exit_code=unit_status.exec_main_status)
        if unit_status.result == "resources":
            return AppRunStatus("failed", reason="env file")
        if unit_status.result in ("signal", "timeout", "start-limit-hit"):
            return AppRunStatus("failed", reason=unit_status.result)
        return AppRunStatus("failed", reason=unit_status.result)
    return AppRunStatus("stopped")


def resolve_running_url(deps: StartDeps, record: AppRecord, *, host: str) -> str | None:
    """Return the app's resolved ``url``, if it declares one -- caller only calls this while ``running``."""
    try:
        manifest = read_manifest_for_app(deps.apps_job_ctx, record)
    except (InvalidManifestSourceError, ManifestValidationError):
        return None
    layout = _layout_for(deps.apps_job_ctx, record)
    try:
        return resolve_url(manifest, record.params, host=host, app_dir=str(layout.cwd))
    except ManifestValidationError:
        return None
