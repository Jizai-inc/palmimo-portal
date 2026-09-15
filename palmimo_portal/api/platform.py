"""``/api/v1/platform``: the realtime platform bundle's readiness and update channel (design doc 2.8).

Distinct from ``/api/v1/update`` (this Portal checkout's own self-update):
the platform bundle is the OS-layer state a Portal build depends on (app
unit template, polkit rules, tmpfiles) -- delivered as a separate
``palmimo-image`` GitHub Release so an existing device can catch up without
a re-flash. ``StartDeps.platform_ready`` (``api/app.py``) reads the same
cached status this router serves, so app install/start refuse with 409
``platform_not_ready`` until the bundle is current.

Gated like ``api/update.py``: never reachable anonymously or during
first-time setup.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from palmimo_portal.api.deps import get_state_store, require_auth, require_full_session, require_provisioned
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core import platform as platform_core
from palmimo_portal.core.platform_update import PlatformUpdateInProgressError
from palmimo_portal.ports import AppsLockTimeoutError, PlatformLockTimeoutError, StateStore


router = APIRouter(
    prefix="/api/v1/platform",
    tags=["platform"],
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)


def _ensure_no_portal_update_in_progress(state_store: StateStore) -> None:
    if state_store.read_update_state().job.state in ("checking", "running", "restarting"):
        raise PortalError(409, "update_in_progress")


def _ensure_no_apps_job_in_progress(state_store: StateStore) -> None:
    try:
        with state_store.lock_apps():
            pass
    except AppsLockTimeoutError as error:
        raise PortalError(409, "app_job_in_progress") from error


class PlatformLatestInfo(BaseModel):
    version: int
    tag: str
    summary: str
    requires_portal: str
    restart_portal: bool
    reflash_required: bool


class PlatformVerifyDiffInfo(BaseModel):
    path: str
    kind: str
    expected: str | None
    actual: str | None


class PlatformStatusResponse(BaseModel):
    installed_version: int | None
    installed_at: str | None
    required_version: int
    ready: bool
    reason: str | None
    verify_diffs: list[PlatformVerifyDiffInfo]
    latest: PlatformLatestInfo | None
    latest_error: str | None


class PlatformJobInfo(BaseModel):
    state: str
    target_version: int | None
    step: str | None
    error: str | None
    started_at: float | None
    finished_at: float | None


class PlatformUpdateAcceptedResponse(BaseModel):
    job: PlatformJobInfo


def _job_info(job: Any) -> PlatformJobInfo:
    return PlatformJobInfo(
        state=job.state,
        target_version=job.target_version,
        step=job.step,
        error=job.error,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _status_response(request: Request, state_store: StateStore, *, force_verify: bool) -> PlatformStatusResponse:
    adapters = request.app.state.adapters
    settings = request.app.state.settings
    installed = adapters.platform.read_installed()

    cache = request.app.state.platform_latest_cache
    ntp_synchronized = adapters.clock.ntp_synchronized()
    latest_manifest, latest_tag, latest_error = cache.get(ntp_synchronized=ntp_synchronized)

    if force_verify:
        diffs = platform_core.run_verify(adapters.platform, settings.platform_dir)
        request.app.state.platform_verify_diffs = diffs
    diffs = request.app.state.platform_verify_diffs

    status = platform_core.compute_status(
        installed, settings.required_platform_version, diffs, latest_manifest, latest_tag, latest_error
    )
    return PlatformStatusResponse(
        installed_version=status["installed_version"],
        installed_at=status["installed_at"],
        required_version=status["required_version"],
        ready=status["ready"],
        reason=status["reason"],
        verify_diffs=[PlatformVerifyDiffInfo(**d) for d in status["verify_diffs"]],
        latest=PlatformLatestInfo(**status["latest"]) if status["latest"] is not None else None,
        latest_error=status["latest_error"],
    )


@router.get("")
def get_platform(
    request: Request, verify: bool = False, state_store: StateStore = Depends(get_state_store)
) -> PlatformStatusResponse:
    """Report the installed platform bundle's version, readiness, and the latest available release.

    ``?verify=true`` re-runs ``install.sh verify`` against the cached
    bundle before answering (design doc 2.8); otherwise the last verify
    result (from startup, the last periodic check, or the last applied
    job) is reused.
    """
    return _status_response(request, state_store, force_verify=verify)


@router.post("/update", status_code=202)
def start_update(
    request: Request, state_store: StateStore = Depends(get_state_store)
) -> PlatformUpdateAcceptedResponse:
    """Fetch and apply the latest platform bundle release in the background.

    Raises:
        PortalError: 409 ``update_in_progress`` / 409 ``app_job_in_progress``
            for the two mutual-exclusion directions with a Portal
            self-update and an app job; 409 ``platform_update_in_progress``
            if a platform update is already running; 502
            ``release_source_unavailable`` if the latest release cannot be
            discovered right now.
    """
    _ensure_no_portal_update_in_progress(state_store)
    _ensure_no_apps_job_in_progress(state_store)

    cache = request.app.state.platform_latest_cache
    ntp_synchronized = request.app.state.adapters.clock.ntp_synchronized()
    manifest, tag, error = cache.get(ntp_synchronized=ntp_synchronized, force=True)
    if manifest is None or tag is None:
        raise PortalError(502, "release_source_unavailable", detail=error)

    runner = request.app.state.platform_job_runner
    try:
        runner.start(tag, manifest.version)
    except (PlatformLockTimeoutError, PlatformUpdateInProgressError) as error:
        raise PortalError(409, "platform_update_in_progress") from error

    return PlatformUpdateAcceptedResponse(job=_job_info(state_store.read_platform_update_state().job))


@router.get("/update")
def get_update_job(state_store: StateStore = Depends(get_state_store)) -> PlatformUpdateAcceptedResponse:
    """Report the current (or last finished) platform-bundle update job."""
    return PlatformUpdateAcceptedResponse(job=_job_info(state_store.read_platform_update_state().job))
