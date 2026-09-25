"""``/api/v1/apps``: install, preview, inspect, configure, update, and delete apps.

Install/update/delete run on a background thread
(:class:`~palmimo_portal.core.apps_job_runner.AppsJobRunner`), which holds
:meth:`~palmimo_portal.ports.StateStore.lock_apps` for the whole pipeline --
see that module's docstring for the lock-handoff-across-threads mechanics.
This router returns 202 with the job's *current* state immediately (running,
or already done/failed if ``PALMIMO_ADAPTERS`` runs it inline for tests);
``GET /apps`` and ``GET /apps/{id}`` report ``installing``/``updating``/
``deleting`` for the app the in-flight job targets, and ``GET
/apps/jobs/{id}`` polls the job itself. A failure that happens *after* the
job starts running (a sync/swap/register error) never reaches this request
-- it lands on the app's ``last_job`` (or is dropped, for a failed install
with nothing to attach it to) for a later poll to see.

Installing is special: the app's ``name`` is not known until its manifest is
read, so :func:`~palmimo_portal.core.apps_jobs.prepare_install_zip`/
``prepare_install_git`` (fetch + read manifest, fast) run synchronously in
this request -- their failures (bad zip/manifest, disk full, a bad git ref)
do surface directly, as 422/507/409. Only the unbounded step (``uv sync``)
in :func:`~palmimo_portal.core.apps_jobs.commit_install` is backgrounded.

``lock_apps`` doubles as the mutual-exclusion signal with a Portal
self-update (``api/update.py`` probes it before starting an apply/rollback);
this router symmetrically checks ``StateStore.read_update_state()`` before
starting a job.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ValidationError, field_validator

from palmimo_portal.api.deps import (
    get_app_unit_port,
    get_apps_job_context,
    get_apps_job_runner,
    get_clock_port,
    get_disk_port,
    get_identity_store,
    get_journal_port,
    get_run_dir_port,
    get_secrets_store,
    get_start_deps,
    get_state_store,
    require_auth,
    require_full_session,
    require_provisioned,
)
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core import apps_jobs
from palmimo_portal.core.apps import (
    InvalidGitSourceError,
    app_namespace,
    clear_credential_rejected,
    host_owner_from_url,
    suggest_app_name,
    validate_git_ref,
    validate_git_subdir_shape,
    validate_git_url,
)
from palmimo_portal.core.apps_diagnostics import AppDiagnosticsInput, build_diagnostics
from palmimo_portal.core.apps_job_runner import AppsJobRunner
from palmimo_portal.core.apps_jobs import AppsJobContext
from palmimo_portal.core.apps_reset import ResetStopError, reset_platform
from palmimo_portal.core.apps_start import (
    RUNNING_ACTIVE_STATES,
    AppBusyError,
    AppJobInProgressStartError,
    AppRunningError,
    AppStartError,
    EnvUnboundError,
    ManifestInvalidError,
    ParamsInvalidError,
    ParamsMissingError,
    PlatformNotReadyError,
    StartDeps,
    VenvMissingError,
    app_unit_name,
    derive_run_status,
    precheck_report,
    resolve_running_url,
    start_app,
    stop_app,
)
from palmimo_portal.core.apps_zip import UPLOAD_MAX_BYTES
from palmimo_portal.core.manifest import (
    InvalidManifestFilenameError,
    Manifest,
    ManifestValidationError,
    validate_manifest_filename,
    validate_param_values,
)
from palmimo_portal.core.platform import compute_status
from palmimo_portal.core.secrets import collect_registered_secret_values, mask_known_values
from palmimo_portal.ports import (
    AdapterUnavailableError,
    AppExistsError,
    AppJob,
    AppNotFoundError,
    AppRecord,
    AppRefKind,
    AppsDirUnavailableError,
    AppsLockTimeoutError,
    AppsState,
    AppsStateFileState,
    AppUnitPort,
    ClockPort,
    DiskFullError,
    DiskPort,
    GitCommandError,
    Identity,
    IdentityStore,
    InvalidManifestSourceError,
    JournalPort,
    PolkitDeniedError,
    RunDirPort,
    RunLockTimeoutError,
    SecretsStore,
    StateStore,
    UnknownSecretNameError,
)
from palmimo_portal.version import portal_version


#: Journal lines attached to a 500 `start_failed` response (design doc pass E).
_START_FAILED_JOURNAL_LINES = 20

#: Journal lines included in `GET /apps/{id}/diagnostics`'s `[journal]` section (design doc 3.2).
_DIAGNOSTICS_JOURNAL_LINES = 50


def _start_failure_journal_tail(journal: JournalPort, name: str, known_secret_values: list[str]) -> list[str]:
    if not journal.can_read():
        return []
    page = journal.read(app_unit_name(name), cursor=None, lines=_START_FAILED_JOURNAL_LINES)
    return [mask_known_values(entry.message, known_secret_values) for entry in page.entries]


logger = logging.getLogger("palmimo_portal")

router = APIRouter(
    prefix="/api/v1/apps",
    tags=["apps"],
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)


def _raise_for_start_error(error: Exception) -> NoReturn:
    """Translate one `core.apps_start.AppStartError` subclass into its 409 `PortalError` (design doc 3.5).

    Each `code` is spelled as a literal here (not read off `error.code`) so
    `tests/test_i18n_parity.py`'s static scan of `PortalError(...)` call
    sites can find it -- see that test's module docstring.
    """
    if isinstance(error, AppBusyError):
        raise PortalError(409, "app_busy") from error
    if isinstance(error, AppJobInProgressStartError):
        raise PortalError(409, "app_job_in_progress") from error
    if isinstance(error, AppRunningError):
        raise PortalError(409, "app_running", running=error.running) from error
    if isinstance(error, ManifestInvalidError):
        raise PortalError(409, "manifest_invalid", errors=error.params.get("errors")) from error
    if isinstance(error, EnvUnboundError):
        raise PortalError(409, "env_unbound", names=error.names) from error
    if isinstance(error, ParamsMissingError):
        raise PortalError(409, "params_missing", names=error.names) from error
    if isinstance(error, ParamsInvalidError):
        raise PortalError(409, "params_invalid", names=error.names) from error
    if isinstance(error, VenvMissingError):
        raise PortalError(409, "venv_missing") from error
    if isinstance(error, PlatformNotReadyError):
        raise PortalError(409, "platform_not_ready") from error
    raise error


def _raise_for_git_error(error: GitCommandError, status_code: int) -> NoReturn:
    """Translate a `GitCommandError`'s `reason` into its literal `PortalError` code.

    Each `code` is spelled as a literal here (not read off `error.reason`) so
    `tests/test_i18n_parity.py`'s static scan of `PortalError(...)` call
    sites can find it -- see `_raise_for_start_error`.
    """
    if error.reason == "git_credential_missing":
        raise PortalError(status_code, "git_credential_missing", detail=str(error)) from error
    if error.reason == "git_credential_rejected":
        raise PortalError(status_code, "git_credential_rejected", detail=str(error)) from error
    if error.reason == "git_not_found":
        raise PortalError(status_code, "git_not_found", detail=str(error)) from error
    if error.reason == "git_commit_mismatch":
        raise PortalError(status_code, "git_commit_mismatch", detail=str(error)) from error
    if error.reason == "git_network_unreachable":
        raise PortalError(status_code, "git_network_unreachable", detail=str(error)) from error
    raise PortalError(status_code, "git_unknown", detail=str(error)) from error


#: UpdateJobState values that mean a Portal self-update is in flight (mirrors api/update.py).
_UPDATE_ACTIVE_STATES = frozenset({"checking", "running", "restarting"})

AppStatus = Literal[
    "stopped",
    "broken",
    "installing",
    "updating",
    "deleting",
    "running",
    "starting",
    "stopping",
    "failed",
    "needs_repair",
]

#: AppJob.kind -> the AppStatus it implies while state.current_job targets this app.
_JOB_KIND_STATUS: dict[str, AppStatus] = {"install": "installing", "update": "updating", "delete": "deleting"}


class AppSourceInfo(BaseModel):
    type: str
    url: str | None
    ref: str | None
    ref_kind: str | None
    subdir: str | None
    commit: str | None
    manifest: str | None
    #: True iff this source normalizes to the official devkit repo (``app_namespace(...) ==
    #: "palmimo"``, design doc 3.9) -- the "official" badge must key off source, not the app id's
    #: namespace segment, so a UI never has to re-derive the normalization rule itself.
    official: bool


class AppJobInfo(BaseModel):
    id: str
    app_id: str | None
    kind: str
    state: str
    step: str | None
    error: str | None
    #: The failure's machine-readable reason (see :attr:`~palmimo_portal.ports.AppJob.error_code`),
    #: for the same per-cause UI guidance a synchronous preview/install git failure gets.
    error_code: str | None
    started_at: float | None
    finished_at: float | None
    lock_generated: bool
    dropped_bindings: list[str]
    dropped_params: list[str]


class AppJobAcceptedResponse(BaseModel):
    job: AppJobInfo


class AppSummary(BaseModel):
    """One app's ledger entry plus a coarse status.

    ``status`` is one of ``stopped``/``broken`` (ledger-derived) or
    ``installing``/``updating``/``deleting`` (this app is the target of the
    one in-flight ``apps.lock`` job). The systemd-derived states
    (``running``, ``failed(exit_code)``) land with start/stop (design doc 3.5).
    """

    name: str
    id: str
    source: AppSourceInfo
    installed_at: float | None
    autostart: bool
    status: AppStatus
    broken_reason: str | None
    credential_rejected: bool
    update_available: bool
    latest_commit: str | None
    last_job: AppJobInfo | None
    exit_code: int | None = None
    reason: str | None = None
    url: str | None = None


class AppsListResponse(BaseModel):
    apps: list[AppSummary]


class EnvSpecInfo(BaseModel):
    name: str
    required: bool
    description: str
    help_url: str | None


class ParamSpecInfo(BaseModel):
    """One declared ``[params.*]`` entry's shape -- not its current stored value (see ``params``)."""

    name: str
    type: str
    default: Any = None
    min: float | None = None
    max: float | None = None
    choices: list[str] | None = None
    pattern: str | None = None
    flag: str | None = None
    description: str | None = None


class AppManifestInfo(BaseModel):
    """The declared manifest shape driving one app's params/env/devices form -- current values live elsewhere."""

    params: list[ParamSpecInfo]
    env: list[EnvSpecInfo]
    devices: list[str]


class AppDetailResponse(BaseModel):
    name: str
    id: str
    source: AppSourceInfo
    installed_at: float | None
    autostart: bool
    status: AppStatus
    broken_reason: str | None
    params: dict[str, Any]
    env: list[EnvSpecInfo]
    bindings: dict[str, str]
    devices: list[str]
    description: str
    manifest: AppManifestInfo | None
    last_job: AppJobInfo | None
    exit_code: int | None = None
    reason: str | None = None
    url: str | None = None


class ManifestPreviewResponse(BaseModel):
    name: str
    namespace: str
    suggested_name: str
    suggested_id: str
    description: str
    devices: list[str]
    env: list[EnvSpecInfo]


class GitSourceRequest(BaseModel):
    type: Literal["git"]
    url: str
    ref: str
    ref_kind: AppRefKind
    subdir: str | None = None
    manifest: str | None = None

    _validate_url = field_validator("url")(validate_git_url)
    _validate_ref = field_validator("ref")(validate_git_ref)
    _validate_subdir = field_validator("subdir")(validate_git_subdir_shape)
    _validate_manifest = field_validator("manifest")(validate_manifest_filename)


class GitInstallRequest(BaseModel):
    source: GitSourceRequest
    name: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return _validate_requested_name(value)


def _validate_requested_name(value: str | None) -> str | None:
    if value is not None and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value) is None:
        raise ValueError("name must match ^[a-z][a-z0-9-]{0,39}$")
    return value


class ParamsRequest(BaseModel):
    params: dict[str, Any]


class BindingsRequest(BaseModel):
    bindings: dict[str, str]


class UpdateCheckResponse(BaseModel):
    update_available: bool
    remote_commit: str | None


class AutostartRequest(BaseModel):
    enabled: bool


class SourceUpdateRequest(BaseModel):
    ref: str
    ref_kind: AppRefKind

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        try:
            return validate_git_ref(value)
        except InvalidGitSourceError as error:
            raise ValueError(str(error)) from error


class StartResponse(BaseModel):
    status: Literal["activating"]


class StopResponse(BaseModel):
    status: Literal["stopping"]


class JournalEntryInfo(BaseModel):
    message: str
    timestamp: float | None
    invocation_id: str | None


class JournalInvocationInfo(BaseModel):
    id: str
    started_at: float | None


class LogsResponse(BaseModel):
    entries: list[JournalEntryInfo] = []
    next_cursor: str | None = None
    invocations: list[JournalInvocationInfo] = []
    unavailable: Literal["journal_permission"] | None = None


#: A systemd invocation id is a 128-bit UUID rendered as 32 lowercase hex digits.
_INVOCATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _ensure_not_corrupt(state_store: StateStore) -> None:
    file_state = state_store.apps_state_file_state()
    if file_state is AppsStateFileState.CORRUPT:
        raise PortalError(409, "platform_state_corrupt")
    if file_state is AppsStateFileState.LEGACY:
        raise PortalError(409, "ledger_legacy")


def _ensure_no_portal_update_in_progress(state_store: StateStore) -> None:
    if state_store.read_update_state().job.state in _UPDATE_ACTIVE_STATES:
        raise PortalError(409, "update_in_progress")


def _ensure_no_platform_update_in_progress(state_store: StateStore) -> None:
    """Refuse an app job while a platform-bundle update job (``api/platform.py``) is running.

    Symmetric with :func:`_ensure_no_portal_update_in_progress`: a platform
    update can change the app-runtime unit template/polkit rules an app job
    also depends on.
    """
    if state_store.read_platform_update_state().job.state == "running":
        raise PortalError(409, "platform_update_in_progress")


def _ensure_no_app_job_in_progress(state: AppsState, name: str) -> None:
    """Refuse a settings write (params/autostart/source-ref) for ``name`` while its own install/update/delete job is running.

    An install/update job completion merges the freshly re-read record back in (see
    :func:`~palmimo_portal.core.apps_job_runner._merge_job_record`), but a delete drops the ledger
    entry outright -- a settings write racing a delete would resurrect it. Mirrors
    :class:`~palmimo_portal.core.apps_start.AppJobInProgressStartError`'s same ``current_job_app``
    check for starting an app.
    """
    if state.current_job_app == name:
        raise PortalError(409, "app_job_in_progress")


def _ensure_platform_ready(deps: StartDeps) -> None:
    """Refuse an install/update while the platform bundle is not ready (design doc 2.7/3.5).

    Mirrors the same gate :func:`~palmimo_portal.core.apps_start.start_app` applies to starting
    an already-installed app -- an app installed/synced against a stale unit
    template/polkit ruleset is a worse failure mode than refusing the job upfront.
    """
    if not deps.platform_ready():
        raise PortalError(409, "platform_not_ready")


def _job_info(job: AppJob, app_id: str | None = None) -> AppJobInfo:
    return AppJobInfo(
        id=job.id,
        app_id=app_id,
        kind=job.kind,
        state=job.state,
        step=job.step,
        error=job.error,
        error_code=job.error_code,
        started_at=job.started_at,
        finished_at=job.finished_at,
        lock_generated=job.lock_generated,
        dropped_bindings=list(job.dropped_bindings),
        dropped_params=list(job.dropped_params),
    )


def _app_id_for_job(state: AppsState, job: AppJob) -> str | None:
    if state.current_job is not None and state.current_job.id == job.id:
        return state.current_job_app
    for app_id, record in state.apps.items():
        if record.last_job is not None and record.last_job.id == job.id:
            return app_id
    if state.last_orphan_job is not None and state.last_orphan_job.id == job.id:
        return state.last_orphan_job_app
    return None


def _source_info(record: AppRecord, catalog_repo: str | None) -> AppSourceInfo:
    return AppSourceInfo(
        type=record.source.type,
        url=record.source.url,
        ref=record.source.ref,
        ref_kind=record.source.ref_kind,
        subdir=record.source.subdir,
        commit=record.source.commit,
        manifest=record.source.manifest,
        official=app_namespace(record.source.type, record.source.url, catalog_repo) == "palmimo",
    )


def _run_status(
    record: AppRecord, state: AppsState, app_unit: AppUnitPort, run_dir: RunDirPort
) -> tuple[AppStatus, int | None, str | None]:
    """Resolve `record`'s status: an in-flight job or `broken` wins over systemd (design doc 3.5).

    Opportunistically removes `record`'s run directory when the unit is
    observed not active/activating/deactivating -- the same condition
    `core.apps_start.reconcile_run_dirs` sweeps for at startup, applied here
    too so a run dir a start left behind (unit exited before the process
    that would clean it up ever ran again) does not wait for the next
    restart to lose its secrets-bearing `env` file.
    """
    if state.current_job is not None and state.current_job_app == record.id:
        job_status = _JOB_KIND_STATUS.get(state.current_job.kind, "stopped")
        return job_status, None, None
    if record.broken_reason is not None:
        return "broken", None, None
    unit_status = app_unit.status(record.id)
    if unit_status.active_state not in RUNNING_ACTIVE_STATES:
        run_dir.remove(record.id)
    run_status = derive_run_status(unit_status)
    return cast(AppStatus, run_status.status), run_status.exit_code, run_status.reason


def _summary(record: AppRecord, state: AppsState, app_unit: AppUnitPort, deps: StartDeps, host: str) -> AppSummary:
    status, exit_code, reason = _run_status(record, state, app_unit, deps.run_dir)
    url = resolve_running_url(deps, record, host=host) if status == "running" else None
    return AppSummary(
        name=record.name,
        id=record.id,
        source=_source_info(record, deps.apps_job_ctx.catalog_repo),
        installed_at=record.installed_at,
        autostart=record.autostart,
        status=status,
        broken_reason=record.broken_reason,
        credential_rejected=record.credential_rejected,
        update_available=record.update_available,
        latest_commit=record.latest_commit,
        last_job=_job_info(record.last_job, record.id) if record.last_job is not None else None,
        exit_code=exit_code,
        reason=reason,
        url=url,
    )


def _pending_install_summary(state: AppsState) -> AppSummary | None:
    """Synthesize a list entry for an install whose manifest name is known but not yet in the ledger."""
    job = state.current_job
    if job is None or job.kind != "install" or state.current_job_app is None or state.current_job_app in state.apps:
        return None
    return AppSummary(
        name=job.display_name or state.current_job_app.rsplit(".", 1)[-1],
        id=state.current_job_app,
        source=AppSourceInfo(
            type="unknown", url=None, ref=None, ref_kind=None, subdir=None, commit=None, manifest=None, official=False
        ),
        installed_at=None,
        autostart=False,
        status="installing",
        broken_reason=None,
        credential_rejected=False,
        update_available=False,
        latest_commit=None,
        last_job=_job_info(job, state.current_job_app),
    )


def _env_info(manifest: Manifest) -> list[EnvSpecInfo]:
    return [
        EnvSpecInfo(name=name, required=spec.required, description=spec.description, help_url=spec.help_url)
        for name, spec in sorted(manifest.env.items())
    ]


def _param_spec_info(manifest: Manifest) -> list[ParamSpecInfo]:
    return [
        ParamSpecInfo(
            name=name,
            type=spec.type,
            default=spec.default if spec.has_default else None,
            min=spec.min,
            max=spec.max,
            choices=spec.choices,
            pattern=spec.pattern,
            flag=spec.flag,
            description=spec.description,
        )
        for name, spec in sorted(manifest.params.items())
    ]


def _manifest_info(manifest: Manifest | None) -> AppManifestInfo | None:
    if manifest is None:
        return None
    return AppManifestInfo(params=_param_spec_info(manifest), env=_env_info(manifest), devices=sorted(manifest.devices))


def _detail(
    record: AppRecord,
    state: AppsState,
    ctx: AppsJobContext,
    secrets: SecretsStore,
    app_unit: AppUnitPort,
    deps: StartDeps,
    host: str,
) -> AppDetailResponse:
    try:
        manifest = apps_jobs.read_manifest_for_app(ctx, record)
    except (InvalidManifestSourceError, ManifestValidationError):
        manifest = None
    status, exit_code, reason = _run_status(record, state, app_unit, deps.run_dir)
    url = resolve_running_url(deps, record, host=host) if status == "running" else None
    return AppDetailResponse(
        name=record.name,
        id=record.id,
        source=_source_info(record, ctx.catalog_repo),
        installed_at=record.installed_at,
        autostart=record.autostart,
        status=status,
        broken_reason=record.broken_reason,
        params=record.params,
        env=_env_info(manifest) if manifest is not None else [],
        bindings=secrets.read_bindings(record.id),
        devices=sorted(manifest.devices) if manifest is not None else [],
        description=manifest.description if manifest is not None else "",
        manifest=_manifest_info(manifest),
        last_job=_job_info(record.last_job, record.id) if record.last_job is not None else None,
        exit_code=exit_code,
        reason=reason,
        url=url,
    )


#: Chunk size for `_read_upload_bounded`'s streaming read -- large enough to not dominate
#: request time with per-chunk overhead, small enough that an oversized body is caught well
#: before it is fully buffered in memory.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_upload_bounded(upload: Any, max_bytes: int) -> Path:
    """Stream `upload` to a fresh temp file in chunks, refusing before or during the read once it
    exceeds `max_bytes` -- design doc 3.4's "fetch" step (`.staging/<id>/upload.zip`) never holds
    the whole body as one in-memory `bytes` object, only `_UPLOAD_CHUNK_BYTES` at a time.

    Checks `upload.size` (the declared size, from the multipart part's own
    byte count) first so an obviously oversized upload never starts a read
    at all; the chunked loop below is the fallback for a part whose
    declared size undercounts the actual bytes.

    Raises:
        PortalError: 422 `zip_too_large`.
    """
    declared_size = getattr(upload, "size", None)
    if declared_size is not None and declared_size > max_bytes:
        raise PortalError(422, "zip_too_large")
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="palmimo-upload-")
    tmp_path = Path(tmp_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PortalError(422, "zip_too_large")
                handle.write(chunk)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


async def _read_zip_or_git(request: Request) -> tuple[Path | None, GitSourceRequest | None, str | None, str | None]:
    """Returns ``(upload_path, git_source, zip_manifest, requested_name)`` -- exactly one of the first two is set.

    ``zip_manifest`` is the validated, optional ``manifest`` form field for a zip install/preview
    (``None`` for a git request, whose own ``manifest`` rides on ``git_source`` instead).
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise PortalError(422, "validation_error", errors=["a 'file' field is required for a zip install"])
        manifest_field = form.get("manifest")
        try:
            zip_manifest = validate_manifest_filename(manifest_field if isinstance(manifest_field, str) else None)
        except InvalidManifestFilenameError as error:
            raise PortalError(422, "validation_error", errors=[str(error)]) from error
        name = form.get("name")
        try:
            requested_name = _validate_requested_name(name if isinstance(name, str) else None)
        except ValueError as error:
            raise PortalError(422, "validation_error", errors=[str(error)]) from error
        return await _read_upload_bounded(upload, UPLOAD_MAX_BYTES), None, zip_manifest, requested_name
    body = await request.json()
    try:
        parsed = GitInstallRequest.model_validate(body)
    except ValidationError as error:
        # `include_context=False`: a `ValueError` raised inside a field validator (e.g.
        # `validate_git_url`) otherwise rides along in `ctx.error` as the raw exception
        # object, which `PortalError`'s JSON envelope cannot serialize.
        raise PortalError(422, "validation_error", errors=error.errors(include_context=False)) from error
    return None, parsed.source, None, parsed.name


def _request_host(request: Request) -> str:
    """The `Host` header's host part -- design doc 3.2: resolves `{host}` to whatever address the caller used."""
    return request.url.hostname or "localhost"


@router.get("")
def list_apps(
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
    deps: StartDeps = Depends(get_start_deps),
) -> AppsListResponse:
    """List every installed app (plus an in-flight install with no ledger entry yet) and its status."""
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    host = _request_host(request)
    summaries = [_summary(record, state, app_unit, deps, host) for _, record in sorted(state.apps.items())]
    pending = _pending_install_summary(state)
    if pending is not None:
        summaries.append(pending)
    return AppsListResponse(apps=sorted(summaries, key=lambda summary: summary.name))


@router.post("/preview")
async def preview(
    request: Request,
    ctx: AppsJobContext = Depends(get_apps_job_context),
    state_store: StateStore = Depends(get_state_store),
) -> ManifestPreviewResponse:
    """Fetch and validate a source without installing it, returning its manifest.

    Raises:
        PortalError: 422 ``validation_error`` for a malformed request body;
            422 ``manifest_invalid`` (with every violation) for a manifest
            that fails validation; 422 ``preview_failed`` for any other
            fetch/extract failure; 422 ``git_credential_missing`` /
            ``git_credential_rejected`` / ``git_not_found`` /
            ``git_network_unreachable`` / ``git_unknown`` for a git source's
            clone/fetch failure.
    """
    upload, git_source, zip_manifest, requested_name = await _read_zip_or_git(request)
    try:
        if upload is not None:
            manifest = apps_jobs.preview_zip(ctx, upload, zip_manifest)
        else:
            assert git_source is not None
            manifest = apps_jobs.preview_git(
                ctx,
                url=git_source.url,
                ref=git_source.ref,
                ref_kind=git_source.ref_kind,
                subdir=git_source.subdir,
                manifest_filename=git_source.manifest,
            )
    except ManifestValidationError as error:
        raise PortalError(422, "manifest_invalid", errors=error.errors) from error
    except InvalidManifestSourceError as error:
        raise PortalError(422, "preview_failed", detail=str(error)) from error
    except GitCommandError as error:
        _raise_for_git_error(error, 422)
    finally:
        if upload is not None:
            upload.unlink(missing_ok=True)
    namespace = app_namespace(
        "zip" if upload is not None else "git", git_source.url if git_source else None, ctx.catalog_repo
    )
    state = state_store.read_apps_state()
    suggested_name = requested_name or suggest_app_name(namespace, manifest.name, state)
    return ManifestPreviewResponse(
        name=manifest.name,
        namespace=namespace,
        suggested_name=suggested_name,
        suggested_id=f"{namespace}.{suggested_name}",
        description=manifest.description,
        devices=sorted(manifest.devices),
        env=_env_info(manifest),
    )


@router.post("/install", status_code=202)
async def install(
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    ctx: AppsJobContext = Depends(get_apps_job_context),
    runner: AppsJobRunner = Depends(get_apps_job_runner),
    deps: StartDeps = Depends(get_start_deps),
) -> AppJobAcceptedResponse:
    """Start installing an app from a zip upload (multipart, field ``file``) or a git source (JSON body).

    ``uv sync`` runs in the background; only the fast fetch+validate half
    runs before this returns. Once accepted, a sync/swap failure surfaces on
    ``GET /apps/jobs/{id}`` or the app's own ``last_job``, not here.

    Raises:
        PortalError: 422 ``manifest_invalid`` / 422 ``install_failed`` /
            422 ``zip_too_large`` / 507 ``disk_full`` for the synchronous
            fetch+validate half; 422 ``git_credential_missing`` /
            ``git_credential_rejected`` / ``git_not_found`` /
            ``git_network_unreachable`` / ``git_unknown`` for a git source's
            clone failure; 409 ``app_exists`` if the manifest's name
            is already installed; 409 ``update_in_progress`` / 409
            ``app_job_in_progress`` / 409 ``platform_not_ready`` for the
            mutual-exclusion and platform-readiness refusals; 503
            ``platform_not_ready`` if ``apps_dir`` itself is unavailable.
    """
    _ensure_not_corrupt(state_store)
    _ensure_no_portal_update_in_progress(state_store)
    _ensure_no_platform_update_in_progress(state_store)
    _ensure_platform_ready(deps)
    upload, git_source, zip_manifest, requested_name = await _read_zip_or_git(request)
    try:
        if upload is not None:
            prepared = apps_jobs.prepare_install_zip(ctx, upload, manifest_filename=zip_manifest)
        else:
            assert git_source is not None
            prepared = apps_jobs.prepare_install_git(
                ctx,
                url=git_source.url,
                ref=git_source.ref,
                ref_kind=git_source.ref_kind,
                subdir=git_source.subdir,
                manifest_filename=git_source.manifest,
            )
    except ManifestValidationError as error:
        raise PortalError(422, "manifest_invalid", errors=error.errors) from error
    except DiskFullError as error:
        raise PortalError(507, "disk_full") from error
    except AppsDirUnavailableError as error:
        raise PortalError(503, "platform_not_ready") from error
    except InvalidManifestSourceError as error:
        raise PortalError(422, "install_failed", detail=str(error)) from error
    except GitCommandError as error:
        _raise_for_git_error(error, 422)
    finally:
        if upload is not None:
            upload.unlink(missing_ok=True)

    try:
        job = runner.start_install(prepared, requested_name=requested_name)
    except AppExistsError as error:
        shutil.rmtree(prepared.staging_container, ignore_errors=True)
        raise PortalError(409, "app_exists") from error
    except AppsLockTimeoutError as error:
        shutil.rmtree(prepared.staging_container, ignore_errors=True)
        raise PortalError(409, "app_job_in_progress") from error
    except AppStartError as error:
        shutil.rmtree(prepared.staging_container, ignore_errors=True)
        _raise_for_start_error(error)
    return AppJobAcceptedResponse(job=_job_info(job, _app_id_for_job(state_store.read_apps_state(), job)))


@router.get("/jobs/{job_id}")
def get_job(job_id: str, state_store: StateStore = Depends(get_state_store)) -> AppJobInfo:
    """Look up one install/update/delete job by id: in flight (``current_job``) or finished (an app's ``last_job``)."""
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    if state.current_job is not None and state.current_job.id == job_id:
        return _job_info(state.current_job, state.current_job_app)
    for record in state.apps.values():
        if record.last_job is not None and record.last_job.id == job_id:
            return _job_info(record.last_job, record.id)
    if state.last_orphan_job is not None and state.last_orphan_job.id == job_id:
        return _job_info(state.last_orphan_job, state.last_orphan_job_app)
    raise PortalError(404, "job_not_found")


@router.get("/{name}")
def get_app(
    name: str,
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    secrets: SecretsStore = Depends(get_secrets_store),
    ctx: AppsJobContext = Depends(get_apps_job_context),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
    deps: StartDeps = Depends(get_start_deps),
) -> AppDetailResponse:
    """Return one app's ledger entry, current manifest, params, and env-binding status."""
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    record = state.apps.get(name)
    if record is None:
        raise PortalError(404, "app_not_found")
    return _detail(record, state, ctx, secrets, app_unit, deps, _request_host(request))


@router.delete("/{name}", status_code=202)
def delete_app(
    name: str,
    state_store: StateStore = Depends(get_state_store),
    runner: AppsJobRunner = Depends(get_apps_job_runner),
) -> AppJobAcceptedResponse:
    """Start deleting an app: remove it from the ledger, then move its files to ``.trash/`` and remove them.

    Raises:
        PortalError: 404 ``app_not_found``; 409 ``update_in_progress`` /
            409 ``app_job_in_progress`` / 409 ``app_running`` (the app must
            be stopped first).
    """
    _ensure_not_corrupt(state_store)
    _ensure_no_portal_update_in_progress(state_store)
    _ensure_no_platform_update_in_progress(state_store)
    try:
        job = runner.start_delete(name)
    except AppNotFoundError as error:
        raise PortalError(404, "app_not_found") from error
    except AppsLockTimeoutError as error:
        raise PortalError(409, "app_job_in_progress") from error
    except AppStartError as error:
        _raise_for_start_error(error)
    return AppJobAcceptedResponse(job=_job_info(job, name))


@router.put("/{name}/params")
def put_params(
    name: str,
    body: ParamsRequest,
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    secrets: SecretsStore = Depends(get_secrets_store),
    ctx: AppsJobContext = Depends(get_apps_job_context),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
    deps: StartDeps = Depends(get_start_deps),
) -> AppDetailResponse:
    """Save an app's param values (design doc 1.2 validation, applied at next start).

    Raises:
        PortalError: 404 ``app_not_found``; 409 ``app_job_in_progress`` while
            an install/update/delete job targets this app; 422
            ``params_invalid`` (with every violation) if a value fails its
            declared type/min/max/pattern/choices.
    """
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    record = state.apps.get(name)
    if record is None:
        raise PortalError(404, "app_not_found")
    _ensure_no_app_job_in_progress(state, name)
    try:
        manifest = apps_jobs.read_manifest_for_app(ctx, record)
    except (InvalidManifestSourceError, ManifestValidationError) as error:
        raise PortalError(409, "app_broken", detail=str(error)) from error
    try:
        validate_param_values(manifest, body.params)
    except ManifestValidationError as error:
        raise PortalError(422, "params_invalid", errors=error.errors) from error
    new_record = replace(record, params={**record.params, **body.params})
    new_state = AppsState(
        apps={**state.apps, name: new_record}, current_job=state.current_job, current_job_app=state.current_job_app
    )
    state_store.write_apps_state(new_state)
    return _detail(new_record, new_state, ctx, secrets, app_unit, deps, _request_host(request))


@router.put("/{name}/bindings")
def put_bindings(
    name: str,
    body: BindingsRequest,
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    secrets: SecretsStore = Depends(get_secrets_store),
    ctx: AppsJobContext = Depends(get_apps_job_context),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
    deps: StartDeps = Depends(get_start_deps),
) -> AppDetailResponse:
    """Replace an app's ``{request_name: registered_secret_name}`` bindings wholesale.

    Raises:
        PortalError: 404 ``app_not_found``; 422 ``unknown_secret_name`` if a
            value names a secret that is not registered.
    """
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    record = state.apps.get(name)
    if record is None:
        raise PortalError(404, "app_not_found")
    try:
        secrets.write_bindings(name, body.bindings)
    except UnknownSecretNameError as error:
        raise PortalError(422, "unknown_secret_name", detail=str(error)) from error
    return _detail(record, state, ctx, secrets, app_unit, deps, _request_host(request))


@router.post("/{name}/update", status_code=202)
def update_app(
    name: str,
    state_store: StateStore = Depends(get_state_store),
    runner: AppsJobRunner = Depends(get_apps_job_runner),
    deps: StartDeps = Depends(get_start_deps),
) -> AppJobAcceptedResponse:
    """Start re-fetching a git-sourced app at its pinned ref and swapping it in.

    Raises:
        PortalError: 404 ``app_not_found``; 409 ``update_in_progress`` /
            409 ``app_job_in_progress`` / 409 ``platform_not_ready`` / 409
            ``app_running`` (the app must be stopped first). A failure
            after the job starts (bad manifest, disk full, sync failure)
            surfaces on the app's ``last_job``, not here.
    """
    _ensure_not_corrupt(state_store)
    _ensure_no_portal_update_in_progress(state_store)
    _ensure_no_platform_update_in_progress(state_store)
    _ensure_platform_ready(deps)
    try:
        job = runner.start_update(name)
    except AppNotFoundError as error:
        raise PortalError(404, "app_not_found") from error
    except AppsLockTimeoutError as error:
        raise PortalError(409, "app_job_in_progress") from error
    except AppStartError as error:
        _raise_for_start_error(error)
    return AppJobAcceptedResponse(job=_job_info(job, name))


@router.post("/{name}/update/check")
def update_check(
    name: str, state_store: StateStore = Depends(get_state_store), ctx: AppsJobContext = Depends(get_apps_job_context)
) -> UpdateCheckResponse:
    """Check a git-sourced app for a new commit/tag upstream (design doc 3.2).

    A successful branch-pinned check clears
    :attr:`~palmimo_portal.ports.AppRecord.credential_rejected` for every
    app sharing this app's ``host/owner`` credential -- a live ``git
    ls-remote`` that succeeds proves the credential is valid again,
    same as design doc 3.6's PUT-based clearing path.

    Raises:
        PortalError: 404 ``app_not_found``; 502 ``git_credential_missing`` /
            ``git_credential_rejected`` / ``git_not_found`` /
            ``git_network_unreachable`` / ``git_unknown`` for a ``git
            ls-remote`` failure.
    """
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    record = state.apps.get(name)
    if record is None:
        raise PortalError(404, "app_not_found")
    try:
        available, remote_commit = apps_jobs.check_git_update(ctx, record)
    except GitCommandError as error:
        _raise_for_git_error(error, 502)
    if record.source.ref_kind == "branch" and record.source.url is not None:
        host_owner = host_owner_from_url(record.source.url)
        if host_owner is not None:
            state_store.write_apps_state(clear_credential_rejected(state_store.read_apps_state(), host_owner))
    return UpdateCheckResponse(update_available=available, remote_commit=remote_commit)


@router.post("/{name}/start", status_code=202)
def start_app_endpoint(
    name: str,
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    deps: StartDeps = Depends(get_start_deps),
    journal: JournalPort = Depends(get_journal_port),
    secrets: SecretsStore = Depends(get_secrets_store),
) -> StartResponse:
    """Run the design doc 3.5 prechecks and start ``name``, changing nothing on any failure.

    Raises:
        PortalError: 404 ``app_not_found``; 409 for every precheck refusal
            (``app_busy``, ``app_running``, ``manifest_invalid``,
            ``env_unbound``, ``params_missing``, ``params_invalid``,
            ``venv_missing``, ``platform_not_ready``); 503 ``polkit_denied``
            if systemd itself refuses the ``set-property``/``start`` verb;
            500 ``start_failed`` (with a ``journal_tail``) for any other
            error the unit start itself raises.
    """
    _ensure_not_corrupt(state_store)
    try:
        start_app(deps, name, host=_request_host(request))
    except AppNotFoundError as error:
        raise PortalError(404, "app_not_found") from error
    except PolkitDeniedError as error:
        raise PortalError(503, "polkit_denied") from error
    except AppStartError as error:
        names = getattr(error, "names", None) or ([error.running] if isinstance(error, AppRunningError) else None)
        logger.warning("app start refused code=%s names=%s", error.code, names)
        _raise_for_start_error(error)
    except AdapterUnavailableError:
        # Left to the app-wide handler (api/app.py) -- a transient D-Bus
        # outage, not a unit-start failure this endpoint should annotate.
        raise
    except Exception as error:
        logger.error("app start failed unexpectedly name=%s: %s", name, error, exc_info=True)
        known_secret_values = collect_registered_secret_values(secrets)
        journal_tail = _start_failure_journal_tail(journal, name, known_secret_values)
        raise PortalError(500, "start_failed", journal_tail=journal_tail) from error
    return StartResponse(status="activating")


@router.post("/{name}/stop", status_code=202)
def stop_app_endpoint(
    name: str, state_store: StateStore = Depends(get_state_store), deps: StartDeps = Depends(get_start_deps)
) -> StopResponse:
    """Stop ``name`` and remove its run directory once ``StopUnit`` completes.

    Raises:
        PortalError: 404 ``app_not_found``; 409 ``app_busy`` if another
            start/stop/autostart already holds ``run.lock``; 503
            ``polkit_denied`` if systemd refuses the ``stop`` verb.
    """
    _ensure_not_corrupt(state_store)
    try:
        stop_app(deps, name)
    except AppNotFoundError as error:
        raise PortalError(404, "app_not_found") from error
    except PolkitDeniedError as error:
        raise PortalError(503, "polkit_denied") from error
    except AppStartError as error:
        _raise_for_start_error(error)
    return StopResponse(status="stopping")


@router.put("/{name}/autostart")
def put_autostart(
    name: str,
    body: AutostartRequest,
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    secrets: SecretsStore = Depends(get_secrets_store),
    ctx: AppsJobContext = Depends(get_apps_job_context),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
    deps: StartDeps = Depends(get_start_deps),
) -> AppDetailResponse:
    """Persist whether ``name`` should be started automatically at Portal boot (design doc 2.5/3.2).

    Raises:
        PortalError: 404 ``app_not_found``; 409 ``app_job_in_progress`` while
            an install/update/delete job targets this app.
    """
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    record = state.apps.get(name)
    if record is None:
        raise PortalError(404, "app_not_found")
    _ensure_no_app_job_in_progress(state, name)
    new_record = replace(record, autostart=body.enabled)
    new_state = AppsState(
        apps={**state.apps, name: new_record}, current_job=state.current_job, current_job_app=state.current_job_app
    )
    state_store.write_apps_state(new_state)
    logger.info("autostart: toggled name=%s enabled=%s", name, body.enabled)
    return _detail(new_record, new_state, ctx, secrets, app_unit, deps, _request_host(request))


@router.put("/{name}/source")
def put_source(
    name: str,
    body: SourceUpdateRequest,
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    secrets: SecretsStore = Depends(get_secrets_store),
    ctx: AppsJobContext = Depends(get_apps_job_context),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
    deps: StartDeps = Depends(get_start_deps),
) -> AppDetailResponse:
    """Change a git-sourced app's pinned ``ref``/``ref_kind``, applied by the next ``update`` (design doc 3.2).

    Raises:
        PortalError: 404 ``app_not_found``; 409 ``app_job_in_progress`` while
            an install/update/delete job targets this app; 409
            ``source_update_git_only`` for a zip-sourced app.
    """
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    record = state.apps.get(name)
    if record is None:
        raise PortalError(404, "app_not_found")
    _ensure_no_app_job_in_progress(state, name)
    if record.source.type != "git":
        raise PortalError(409, "source_update_git_only")
    new_record = replace(record, source=replace(record.source, ref=body.ref, ref_kind=body.ref_kind))
    new_state = AppsState(
        apps={**state.apps, name: new_record}, current_job=state.current_job, current_job_app=state.current_job_app
    )
    state_store.write_apps_state(new_state)
    return _detail(new_record, new_state, ctx, secrets, app_unit, deps, _request_host(request))


@router.get("/{name}/logs")
def get_logs(
    name: str,
    cursor: str | None = None,
    lines: int = Query(default=200, ge=1, le=1000),
    invocation: str | None = None,
    state_store: StateStore = Depends(get_state_store),
    journal: JournalPort = Depends(get_journal_port),
    secrets: SecretsStore = Depends(get_secrets_store),
) -> LogsResponse:
    """Return up to ``lines`` journal entries for ``name``'s unit, cursor-paginated (design doc 3.2/3.8).

    ``invocations`` lists every distinct systemd invocation id seen, so the
    UI can offer "view a previous start". Answers ``{"unavailable":
    "journal_permission"}`` rather than an error when this process cannot
    read the journal at all (design doc 3.6). Every entry's ``message`` is
    masked the same way ``GET /apps/{id}/diagnostics`` is -- an app can
    print a registered secret or git credential to its own journal.

    PortalError: 400 ``invalid_invocation`` if ``invocation`` is not a
    32-hex-digit systemd invocation id; 404 ``app_not_found``.
    """
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    if name not in state.apps:
        raise PortalError(404, "app_not_found")
    if invocation is not None and not _INVOCATION_ID_PATTERN.fullmatch(invocation):
        raise PortalError(400, "invalid_invocation")
    if not journal.can_read():
        return LogsResponse(unavailable="journal_permission")
    page = journal.read(app_unit_name(name), cursor=cursor, lines=lines, invocation=invocation)
    known_secret_values = collect_registered_secret_values(secrets)
    return LogsResponse(
        entries=[
            JournalEntryInfo(
                message=mask_known_values(entry.message, known_secret_values),
                timestamp=entry.timestamp,
                invocation_id=entry.invocation_id,
            )
            for entry in page.entries
        ],
        next_cursor=page.next_cursor,
        invocations=[
            JournalInvocationInfo(id=invocation.id, started_at=invocation.started_at) for invocation in page.invocations
        ],
    )


class ResetResponse(BaseModel):
    paths: list[str]
    #: Paths reset could not remove even after escalating to the sync unit's privileged
    #: "purge mode" (`core.apps_jobs.purge_path`) -- empty on a fully clean reset.
    leftover_paths: list[str]


@router.get("/{name}/diagnostics", response_class=PlainTextResponse)
def get_diagnostics(
    name: str,
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    secrets: SecretsStore = Depends(get_secrets_store),
    ctx: AppsJobContext = Depends(get_apps_job_context),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
    deps: StartDeps = Depends(get_start_deps),
    journal: JournalPort = Depends(get_journal_port),
    disk: DiskPort = Depends(get_disk_port),
    clock: ClockPort = Depends(get_clock_port),
    identity: IdentityStore = Depends(get_identity_store),
) -> PlainTextResponse:
    """Return one flat support document for ``name`` (design doc 3.2): versions, source, prechecks, logs.

    Never contains a secret value or git token, even if an app printed one
    to its own journal -- the whole document is run through the same
    masking pass every registered secret value and git credential token
    goes through.

    Raises:
        PortalError: 404 ``app_not_found``.
    """
    _ensure_not_corrupt(state_store)
    state = state_store.read_apps_state()
    record = state.apps.get(name)
    if record is None:
        raise PortalError(404, "app_not_found")

    try:
        manifest = apps_jobs.read_manifest_for_app(ctx, record)
        manifest_errors: list[str] = []
    except ManifestValidationError as error:
        manifest = None
        manifest_errors = error.errors
    except InvalidManifestSourceError as error:
        manifest = None
        manifest_errors = [str(error)]

    app_state: Any = request.app.state
    settings = app_state.settings
    platform_status = compute_status(
        app_state.adapters.platform.read_installed(),
        settings.required_platform_version,
        app_state.platform_verify_diffs,
        None,
        None,
        None,
    )

    journal_lines: list[str] | None = None
    if journal.can_read():
        page = journal.read(app_unit_name(record.id), cursor=None, lines=_DIAGNOSTICS_JOURNAL_LINES)
        journal_lines = [entry.message for entry in page.entries]

    identity_info = identity.read_identity_uncached()
    data = AppDiagnosticsInput(
        portal_version=portal_version(),
        platform_status=platform_status,
        uv_version=ctx.uv.version(),
        record=record,
        manifest=manifest,
        manifest_errors=manifest_errors,
        bindings=secrets.read_bindings(record.id),
        precheck=precheck_report(deps, record.id, host=_request_host(request)),
        unit_status=app_unit.status(record.id),
        journal_lines=journal_lines,
        disk_free_bytes=disk.free_bytes(settings.state_dir),
        ntp_synchronized=clock.ntp_synchronized(),
        hostname=socket.gethostname(),
        device_id=identity_info.device_id if isinstance(identity_info, Identity) else None,
        portal_update_job_state=state_store.read_update_state().job.state,
    )
    text = build_diagnostics(data, known_secret_values=collect_registered_secret_values(secrets))
    return PlainTextResponse(text)


@router.post("/reset", status_code=202)
def reset_apps(
    state_store: StateStore = Depends(get_state_store),
    ctx: AppsJobContext = Depends(get_apps_job_context),
    app_unit: AppUnitPort = Depends(get_app_unit_port),
    run_dir: RunDirPort = Depends(get_run_dir_port),
    secrets: SecretsStore = Depends(get_secrets_store),
) -> ResetResponse:
    """Stop every running app and erase the app platform back to its just-installed state (design doc 3.2).

    The only SSH-free recovery from a corrupt/unreadable app ledger.
    Refuses while any app job, Portal self-update, or platform-bundle
    update is running, or while a start/stop is in flight -- serialized by
    taking both ``apps.lock`` and ``run.lock`` for the whole operation.

    Raises:
        PortalError: 409 ``app_job_in_progress`` (an app job is running, or
            ``apps.lock`` is otherwise held); 409 ``app_busy`` (a
            start/stop/autostart holds ``run.lock``); 409
            ``update_in_progress`` / 409 ``platform_update_in_progress``.
    """
    _ensure_no_portal_update_in_progress(state_store)
    _ensure_no_platform_update_in_progress(state_store)
    try:
        with state_store.lock_apps(), state_store.lock_run():
            result = reset_platform(state_store, ctx, app_unit, run_dir, secrets)
    except AppsLockTimeoutError as error:
        raise PortalError(409, "app_job_in_progress") from error
    except RunLockTimeoutError as error:
        raise PortalError(409, "app_busy") from error
    except ResetStopError as error:
        raise PortalError(409, "app_stop_failed") from error
    return ResetResponse(paths=result.deleted, leftover_paths=result.leftover_paths)
