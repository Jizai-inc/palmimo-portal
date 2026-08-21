"""``/api/v1/update``: check GitHub Releases, apply one at a time, and roll back.

Update means this Portal checkout, one GitHub Release at a time: discover
via ``releases/latest``, apply with ``git fetch`` -> ``git checkout`` ->
``uv sync``, then restart the Portal. No channel selection, arbitrary
tags, or OS/apt updates. Power loss mid-update is a known open item; see
``core/update.py`` and ``core/update_runner.py`` for durability and
rollback handling.

Gated like ``system/reboot``/``system/shutdown``
(``require_provisioned`` + ``require_auth`` + ``require_full_session``):
never reachable anonymously or during first-time setup.
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from palmimo_portal.api.deps import (
    get_release_source,
    get_state_store,
    get_update_lock,
    get_updater,
    require_auth,
    require_full_session,
    require_provisioned,
)
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core import update as update_core
from palmimo_portal.ports import (
    InstalledVersion,
    ReleaseSource,
    ReleaseSourceError,
    StateStore,
    Updater,
    UpdateState,
)
from palmimo_portal.settings import Settings


logger = logging.getLogger("palmimo_portal")


router = APIRouter(
    prefix="/api/v1/update",
    tags=["update"],
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)


class InstalledInfo(BaseModel):
    tag: str | None
    commit: str | None


class ReleaseInfo(BaseModel):
    tag: str
    name: str
    published_at: str
    html_url: str


class UpdateJobInfo(BaseModel):
    kind: str
    state: str
    target: str | None
    step: str | None
    error: str | None
    started_at: float | None
    finished_at: float | None
    restarting_at: float | None


class UpdateStatusResponse(BaseModel):
    installed: InstalledInfo
    latest: ReleaseInfo | None
    checked_at: float | None
    update_available: bool
    previous_tag: str | None
    job: UpdateJobInfo
    retry_available: bool


class ApplyRequest(BaseModel):
    tag: str


def _status_response(state: UpdateState, installed: InstalledVersion, channel: str) -> UpdateStatusResponse:
    return UpdateStatusResponse(
        installed=InstalledInfo(tag=installed.tag, commit=installed.commit),
        latest=(
            ReleaseInfo(
                tag=state.latest.tag,
                name=state.latest.name,
                published_at=state.latest.published_at,
                html_url=state.latest.html_url,
            )
            if state.latest is not None
            else None
        ),
        checked_at=state.checked_at,
        update_available=update_core.is_update_available(installed, state.latest, channel=channel),
        previous_tag=state.previous_tag,
        retry_available=update_core.is_retry_available(state.job, state.latest),
        job=UpdateJobInfo(
            kind=state.job.kind,
            state=state.job.state,
            target=state.job.target,
            step=state.job.step,
            error=state.job.error,
            started_at=state.job.started_at,
            finished_at=state.job.finished_at,
            restarting_at=state.job.restarting_at,
        ),
    )


@router.get("/status")
def get_status(
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    updater: Updater = Depends(get_updater),
    lock: threading.Lock = Depends(get_update_lock),
) -> UpdateStatusResponse:
    """Report the installed version, the last-checked release, and any in-progress job.

    Self-heals the persisted state first via
    :func:`~palmimo_portal.core.update.current_update_state`: expires a
    stale ``"restarting"`` job (else it polls as "restarting" forever) and
    a ``"running"`` job with no live runner in this process (a write
    failure can kill the runner thread, stranding the job).
    """
    with lock:
        runner_alive = request.app.state.update_runner_alive.is_set()
        state = update_core.current_update_state(state_store, now=time.time(), runner_alive=runner_alive)
    settings: Settings = request.app.state.settings
    return _status_response(state, updater.installed(), channel=settings.update_channel)


@router.post("/check")
def check(
    request: Request,
    release_source: ReleaseSource = Depends(get_release_source),
    state_store: StateStore = Depends(get_state_store),
    updater: Updater = Depends(get_updater),
    lock: threading.Lock = Depends(get_update_lock),
) -> UpdateStatusResponse:
    """Fetch the latest GitHub Release synchronously (up to a 10s timeout) and persist it.

    Read-only with respect to ``job``: a check never starts, clears, or
    otherwise touches the update job, so a ``done``/``failed`` job from an
    earlier apply/rollback stays visible (with its own ``retry_available``)
    across any number of checks.

    Raises:
        PortalError: 409 ``update_in_progress`` while a job is already
            running/restarting/checking; 429 ``update_check_rate_limited``
            (with ``retry_after_seconds``) if the last check was under a
            minute ago; 404 ``no_release`` / 502
            ``release_source_unavailable`` -- mapped 1:1 from
            :class:`~palmimo_portal.ports.ReleaseSourceError`'s ``code``;
            502 ``release_source_unavailable`` again if the release
            source's ``tag_name`` is not
            :func:`~palmimo_portal.core.update.is_valid_release_tag` -- a
            malformed tag is exactly as unusable to this Portal as GitHub
            itself being unreachable.
    """
    with lock:
        state = state_store.read_update_state()
        try:
            update_core.start_check(state, now=time.time())
        except update_core.UpdateInProgressError as error:
            raise PortalError(409, "update_in_progress") from error
        except update_core.UpdateCheckRateLimitedError as error:
            raise PortalError(
                429, "update_check_rate_limited", retry_after_seconds=error.retry_after_seconds
            ) from error

        try:
            latest = release_source.fetch_latest()
        except ReleaseSourceError as error:
            # Literal PortalError calls, not built from error.code: the
            # i18n-parity scan (tests/test_i18n_parity.py) only recognizes
            # literal string `code` arguments.
            if error.code == "no_release":
                raise PortalError(404, "no_release") from error
            raise PortalError(502, "release_source_unavailable") from error

        try:
            state = update_core.record_latest(state, latest, now=time.time())
        except update_core.InvalidReleaseTagError as error:
            logger.warning("update: check received an invalid release tag %r -- refusing to store it", latest.tag)
            raise PortalError(502, "release_source_unavailable") from error
        state_store.write_update_state(state)
    settings: Settings = request.app.state.settings
    return _status_response(state, updater.installed(), channel=settings.update_channel)


@router.post("/apply", status_code=202)
def apply(
    body: ApplyRequest,
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    updater: Updater = Depends(get_updater),
    lock: threading.Lock = Depends(get_update_lock),
) -> UpdateStatusResponse:
    """Start applying ``tag`` in the background and return immediately.

    Raises:
        PortalError: 409 ``update_in_progress``; 400 ``invalid_release_tag``
            if ``tag`` is not a safe ``git``/``uv`` argument; 409
            ``prerelease_refused`` if ``tag`` is a pre-release tag on the
            stable channel; 409 ``no_release_checked`` if no release has
            ever been checked; 409 ``update_target_mismatch`` if ``tag`` is
            not the last-checked release's tag.
    """
    settings: Settings = request.app.state.settings
    with lock:
        state = state_store.read_update_state()
        installed = updater.installed()
        try:
            state = update_core.start_apply(
                state, installed, body.tag, now=time.time(), channel=settings.update_channel
            )
        except update_core.UpdateInProgressError as error:
            raise PortalError(409, "update_in_progress") from error
        except update_core.InvalidReleaseTagError as error:
            raise PortalError(400, "invalid_release_tag") from error
        except update_core.PrereleaseRefusedError as error:
            raise PortalError(409, "prerelease_refused") from error
        except update_core.NoReleaseCheckedError as error:
            raise PortalError(409, "no_release_checked") from error
        except update_core.UpdateTargetMismatch as error:
            raise PortalError(409, "update_target_mismatch") from error
        state_store.write_update_state(state)

    _start_runner(request, body.tag)
    return _status_response(state_store.read_update_state(), updater.installed(), channel=settings.update_channel)


@router.post("/rollback", status_code=202)
def rollback(
    request: Request,
    state_store: StateStore = Depends(get_state_store),
    updater: Updater = Depends(get_updater),
    lock: threading.Lock = Depends(get_update_lock),
) -> UpdateStatusResponse:
    """Start rolling back to the previous tag in the background and return immediately.

    Raises:
        PortalError: 409 ``update_in_progress``; 409 ``no_previous_version``
            if there is no previous tag to roll back to; 400
            ``invalid_release_tag`` if the previous tag is not a safe
            ``git``/``uv`` argument (defense in depth -- see
            :func:`~palmimo_portal.core.update.start_rollback`'s docstring);
            409 ``prerelease_refused`` if the previous tag is a pre-release
            tag on the stable channel.
    """
    with lock:
        state = state_store.read_update_state()
        installed = updater.installed()
        try:
            state = update_core.start_rollback(state, installed, now=time.time())
        except update_core.UpdateInProgressError as error:
            raise PortalError(409, "update_in_progress") from error
        except update_core.NoPreviousVersionError as error:
            raise PortalError(409, "no_previous_version") from error
        except update_core.InvalidReleaseTagError as error:
            raise PortalError(400, "invalid_release_tag") from error
        except update_core.PrereleaseRefusedError as error:
            raise PortalError(409, "prerelease_refused") from error
        state_store.write_update_state(state)

    target = state.job.target
    assert target is not None  # start_rollback always sets job.target = previous_tag
    settings: Settings = request.app.state.settings
    _start_runner(request, target)
    return _status_response(state_store.read_update_state(), updater.installed(), channel=settings.update_channel)


def _start_runner(request: Request, target: str) -> None:
    """Start *target* applying on the app's shared :class:`~palmimo_portal.core.update_runner.UpdateRunner`.

    Uses the single instance constructed at app startup (``api/app.py``'s
    ``create_app``) so its ``_busy_lock`` guards across requests and jobs.
    """
    runner = request.app.state.update_runner
    runner.start(target)
