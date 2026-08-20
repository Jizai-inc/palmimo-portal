"""Pure state-transition rules for the update job: check -> apply/rollback -> restart -> finalize.

The whole lifecycle lives in one :class:`~palmimo_portal.ports.UpdateState`,
persisted via :class:`~palmimo_portal.ports.StateStore`. Every transition
here is a pure function -- old state in, new state out, or an exception
when not allowed -- so ``api/update.py`` and
:mod:`palmimo_portal.core.update_runner` share one implementation.

State machine: ``idle`` -> :func:`start_check`/:func:`record_latest`
(synchronous, returns to ``idle``) -> :func:`start_apply`/:func:`start_rollback`
(``"running"``) -> :func:`advance` per :class:`~palmimo_portal.ports.Updater`
step -> :func:`mark_restarting` (apply succeeded, about to restart) ->
:func:`finalize_after_restart` (once at startup: resolves ``"restarting"``
into ``"done"``/``"failed"``, and fails a ``"running"``/``"checking"`` job
orphaned by a process that died before this boot). :func:`mark_failed`
covers any step failing along the way.
"""

from __future__ import annotations

import re

from palmimo_portal.ports import InstalledVersion, Release, StateStore, UpdateJob, UpdateState


#: How long a successful check protects the Portal from another one.
CHECK_RATE_LIMIT_SECONDS = 60.0

#: Single source of truth for the frontend build's GitHub Release asset name.
#: The release workflow, ``GitUvUpdater``'s ``assets`` step, and the
#: Makefile's ``fetch-static`` target all derive from this pattern.
STATIC_ASSET_NAME_TEMPLATE = "palmimo-portal-static-{tag}.tar.gz"

#: Roughly what ``git check-ref-format`` accepts, tightened to what this
#: feature needs to shell out safely. The ``.lock``/``..`` checks in
#: :func:`is_valid_release_tag` catch two git-specific traps this pattern alone would not.
_VALID_RELEASE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: The only :class:`~palmimo_portal.ports.UpdateJobState` values a new
#: check/apply/rollback may start from -- anything else means a job is already in flight.
_ALLOWS_NEW_JOB_STATES = frozenset({"idle", "done", "failed"})

IDLE_UPDATE_JOB = UpdateJob(
    state="idle", kind="update", target=None, step=None, error=None, started_at=None, finished_at=None
)
IDLE_UPDATE_STATE = UpdateState(latest=None, checked_at=None, previous_tag=None, job=IDLE_UPDATE_JOB)


class UpdateInProgressError(Exception):
    """Raised by :func:`start_check`/:func:`start_apply`/:func:`start_rollback` when a job is already in flight."""


class UpdateCheckRateLimitedError(Exception):
    """Raised by :func:`start_check` when the last check was under :data:`CHECK_RATE_LIMIT_SECONDS` ago."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"retry after {retry_after_seconds:.0f}s")


class NoReleaseCheckedError(Exception):
    """Raised by :func:`start_apply` when no release has ever been checked (``state.latest is None``)."""


class UpdateTargetMismatch(Exception):  # noqa: N818 -- named to match the design doc's own term for this case
    """Raised by :func:`start_apply` when ``target`` is not the last-checked release's tag."""


class NoPreviousVersionError(Exception):
    """Raised by :func:`start_rollback` when there is no ``previous_tag`` to roll back to."""


class InvalidReleaseTagError(Exception):
    """Raised by :func:`record_latest`/:func:`start_apply`/:func:`start_rollback` for a malformed tag.

    Defense in depth with :func:`is_valid_release_tag`: guarded once from
    the GitHub API response, and again when a tag becomes an update job's
    ``target``.
    """


def is_valid_release_tag(tag: str) -> bool:
    """Report whether ``tag`` is safe to pass to ``git``/``uv`` as a ref/argument. See :data:`_VALID_RELEASE_TAG_PATTERN`."""
    if not _VALID_RELEASE_TAG_PATTERN.fullmatch(tag):
        return False
    if tag.endswith(".lock"):
        return False
    return ".." not in tag


def is_update_available(installed: InstalledVersion, latest: Release | None) -> bool:
    """Report whether ``latest`` names a release the installed checkout is not already on.

    ``installed.tag is None`` (``HEAD`` not exactly on a tag) is treated
    as "always behind" whenever a latest release is known.
    """
    if latest is None:
        return False
    if installed.tag is None:
        return True
    return latest.tag != installed.tag


def start_check(state: UpdateState, now: float) -> UpdateState:
    """Begin a release check, or raise if one cannot start right now.

    Raises:
        UpdateInProgressError: a job is already running/restarting/checking.
        UpdateCheckRateLimitedError: the last check was under
            :data:`CHECK_RATE_LIMIT_SECONDS` ago.
    """
    if state.job.state not in _ALLOWS_NEW_JOB_STATES:
        raise UpdateInProgressError()
    if state.checked_at is not None:
        elapsed = now - state.checked_at
        # A negative elapsed time means the wall clock stepped backwards
        # (e.g. a power-cut reboot with no RTC) -- treat that as an expired
        # rate limit, the same rule resolve_attempt uses (core/wifi_attempt.py).
        if 0 <= elapsed < CHECK_RATE_LIMIT_SECONDS:
            raise UpdateCheckRateLimitedError(CHECK_RATE_LIMIT_SECONDS - elapsed)
    return UpdateState(
        latest=state.latest,
        checked_at=state.checked_at,
        previous_tag=state.previous_tag,
        job=UpdateJob(
            state="checking", kind=state.job.kind, target=None, step=None, error=None, started_at=now, finished_at=None
        ),
    )


def record_latest(state: UpdateState, latest: Release, now: float) -> UpdateState:
    """Record a freshly fetched release and return the job to idle.

    Raises:
        InvalidReleaseTagError: ``latest.tag`` is not :func:`is_valid_release_tag`.
    """
    if not is_valid_release_tag(latest.tag):
        raise InvalidReleaseTagError()
    return UpdateState(latest=latest, checked_at=now, previous_tag=state.previous_tag, job=IDLE_UPDATE_JOB)


def is_retry_available(job: UpdateJob, latest: Release | None) -> bool:
    """Report whether a failed job can be retried by re-applying its own ``target``.

    True only when the job failed and its ``target`` is still the
    last-checked release's tag -- covers the tag-already-installed
    uv-sync-failure case where :func:`is_update_available` would otherwise
    say no update is available and hide the retry option.
    """
    return job.state == "failed" and latest is not None and job.target == latest.tag


def start_apply(state: UpdateState, installed: InstalledVersion, target: str, now: float) -> UpdateState:
    """Begin applying ``target``, or raise if it cannot start right now.

    ``previous_tag`` is set to ``installed.tag`` only when not ``None`` and different from
    ``target``; otherwise kept, so a non-tagged checkout or a retry after a failed apply does
    not clobber the "go back to" tag.

    Raises:
        UpdateInProgressError: a job is already running/restarting/checking.
        InvalidReleaseTagError: ``target`` is not :func:`is_valid_release_tag`.
        NoReleaseCheckedError: ``state.latest is None``.
        UpdateTargetMismatch: ``target`` is not ``state.latest.tag``.
    """
    if state.job.state not in _ALLOWS_NEW_JOB_STATES:
        raise UpdateInProgressError()
    if not is_valid_release_tag(target):
        raise InvalidReleaseTagError()
    if state.latest is None:
        raise NoReleaseCheckedError()
    if target != state.latest.tag:
        raise UpdateTargetMismatch()
    previous_tag = installed.tag if installed.tag is not None and installed.tag != target else state.previous_tag
    return UpdateState(
        latest=state.latest,
        checked_at=state.checked_at,
        previous_tag=previous_tag,
        job=UpdateJob(
            state="running", kind="update", target=target, step=None, error=None, started_at=now, finished_at=None
        ),
    )


def start_rollback(state: UpdateState, installed: InstalledVersion, now: float) -> UpdateState:
    """Begin rolling back to ``state.previous_tag``, or raise if it cannot start right now.

    ``previous_tag`` becomes the tag being *left* (``installed.tag``) once
    the rollback completes, the same way :func:`start_apply` sets it for
    the forward direction, so the dashboard's rollback card flips into a
    "go forward again" card once the rollback lands. Only set when
    ``installed.tag`` is not ``None`` and differs from the rollback target.

    Raises:
        UpdateInProgressError: a job is already running/restarting/checking.
        NoPreviousVersionError: ``state.previous_tag`` is ``None``.
        InvalidReleaseTagError: ``state.previous_tag`` is not :func:`is_valid_release_tag`
            (defense in depth; should not happen since it is only ever set from an already-validated tag).
    """
    if state.job.state not in _ALLOWS_NEW_JOB_STATES:
        raise UpdateInProgressError()
    if state.previous_tag is None:
        raise NoPreviousVersionError()
    if not is_valid_release_tag(state.previous_tag):
        raise InvalidReleaseTagError()
    target = state.previous_tag
    previous_tag = installed.tag if installed.tag is not None and installed.tag != target else state.previous_tag
    return UpdateState(
        latest=state.latest,
        checked_at=state.checked_at,
        previous_tag=previous_tag,
        job=UpdateJob(
            state="running",
            kind="rollback",
            target=target,
            step=None,
            error=None,
            started_at=now,
            finished_at=None,
        ),
    )


def advance(state: UpdateState, step: str) -> UpdateState:
    """Record that the running job has reached ``step`` (``"fetch"``/``"assets"``/``"checkout"``/``"sync"``/``"install-assets"``)."""
    job = state.job
    return UpdateState(
        latest=state.latest,
        checked_at=state.checked_at,
        previous_tag=state.previous_tag,
        job=UpdateJob(
            state="running",
            kind=job.kind,
            target=job.target,
            step=step,
            error=None,
            started_at=job.started_at,
            finished_at=None,
        ),
    )


def mark_restarting(state: UpdateState, now: float) -> UpdateState:
    """Record that ``apply`` succeeded and the Portal is about to restart itself.

    ``now`` is stamped onto ``restarting_at`` (the restart-request moment,
    not ``job.started_at``) so :func:`expire_stale_restart`'s timeout does
    not eat into a slow but healthy ``apply``'s budget.
    """
    job = state.job
    return UpdateState(
        latest=state.latest,
        checked_at=state.checked_at,
        previous_tag=state.previous_tag,
        job=UpdateJob(
            state="restarting",
            kind=job.kind,
            target=job.target,
            step=job.step,
            error=None,
            started_at=job.started_at,
            finished_at=None,
            restarting_at=now,
        ),
    )


def mark_failed(state: UpdateState, step: str, error: str, now: float) -> UpdateState:
    """Record that the running job failed at ``step``."""
    job = state.job
    return UpdateState(
        latest=state.latest,
        checked_at=state.checked_at,
        previous_tag=state.previous_tag,
        job=UpdateJob(
            state="failed",
            kind=job.kind,
            target=job.target,
            step=step,
            error=error,
            started_at=job.started_at,
            finished_at=now,
        ),
    )


#: How long a "restarting" job may sit before :func:`expire_stale_restart` fails it out.
DEFAULT_RESTART_MAX_AGE_SECONDS = 600.0


def expire_stale_restart(
    state: UpdateState, now: float, max_age_seconds: float = DEFAULT_RESTART_MAX_AGE_SECONDS
) -> UpdateState:
    """Fail a ``"restarting"`` job that has sat for longer than ``max_age_seconds``.

    :func:`finalize_after_restart` runs once, at startup, and says nothing
    about a restart that never happens (systemd never brings the process
    back up), which would otherwise sit ``"restarting"`` forever and block
    any new check/apply/rollback (:data:`_ALLOWS_NEW_JOB_STATES`). Callers
    are expected to call this at the top of every ``GET /update/status``,
    not only at startup, so staleness is caught on the next poll.

    Returns ``state`` unchanged (identity-comparable) unless the job is
    ``"restarting"`` and ``now - reference >= max_age_seconds``, where
    ``reference`` is ``job.restarting_at`` (falling back to
    ``job.started_at`` only when ``None``) -- measured from the restart
    itself, not the whole preceding apply, so a legitimately slow apply is
    not expired the moment it becomes ``"restarting"``.
    """
    if state.job.state != "restarting":
        return state
    job = state.job
    reference = job.restarting_at if job.restarting_at is not None else job.started_at
    if reference is None or now - reference < max_age_seconds:
        return state
    new_job = UpdateJob(
        state="failed",
        kind=job.kind,
        target=job.target,
        step="restart",
        error="the Portal did not restart within 10 minutes -- reboot from the Power screen",
        started_at=job.started_at,
        finished_at=now,
        restarting_at=job.restarting_at,
    )
    return UpdateState(latest=state.latest, checked_at=state.checked_at, previous_tag=state.previous_tag, job=new_job)


def finalize_after_restart(state: UpdateState, installed: InstalledVersion, now: float) -> UpdateState:
    """Resolve a leftover job at startup, or return ``state`` unchanged if there is none.

    Called once at Portal startup (normal boot or update-triggered
    restart) -- a job in ``"idle"``/``"done"``/``"failed"`` is left
    untouched. Two states are not, since no thread survives to resolve
    them otherwise and :data:`_ALLOWS_NEW_JOB_STATES` would wedge the job
    forever: ``"restarting"`` compares ``installed.tag`` against the job's
    own ``target`` (recorded before the restart) and resolves to
    ``"done"``/``"failed"``; ``"running"``/``"checking"`` is marked
    ``"failed"`` (the process that was working on it is gone). A third
    case: a job already ``"failed"`` at ``step == "restart"`` (written by
    :func:`expire_stale_restart` giving up on a slow restart) is promoted
    to ``"done"`` when ``installed.tag`` matches ``target`` -- the restart
    landed after all, just later than the timeout allowed.
    """
    if state.job.state in ("running", "checking"):
        job = state.job
        new_job = UpdateJob(
            state="failed",
            kind=job.kind,
            target=job.target,
            step=job.step or "start",
            error="interrupted: the Portal restarted before this job finished",
            started_at=job.started_at,
            finished_at=now,
        )
        return UpdateState(
            latest=state.latest, checked_at=state.checked_at, previous_tag=state.previous_tag, job=new_job
        )
    if state.job.state == "failed" and state.job.step == "restart" and state.job.target is not None:
        job = state.job
        if installed.tag == job.target:
            new_job = UpdateJob(
                state="done",
                kind=job.kind,
                target=job.target,
                step=None,
                error=None,
                started_at=job.started_at,
                finished_at=now,
                restarting_at=job.restarting_at,
            )
            return UpdateState(
                latest=state.latest, checked_at=state.checked_at, previous_tag=state.previous_tag, job=new_job
            )
        return state
    if state.job.state != "restarting":
        return state
    job = state.job
    if installed.tag == job.target:
        new_job = UpdateJob(
            state="done",
            kind=job.kind,
            target=job.target,
            step=None,
            error=None,
            started_at=job.started_at,
            finished_at=now,
            restarting_at=job.restarting_at,
        )
    else:
        observed = installed.tag or installed.commit or "unknown"
        error = f"restarted on {observed}, expected {job.target}"
        new_job = UpdateJob(
            state="failed",
            kind=job.kind,
            target=job.target,
            step="restart",
            error=error,
            started_at=job.started_at,
            finished_at=now,
            restarting_at=job.restarting_at,
        )
    return UpdateState(latest=state.latest, checked_at=state.checked_at, previous_tag=state.previous_tag, job=new_job)


def expire_stale_running(state: UpdateState, now: float, runner_alive: bool) -> UpdateState:
    """Fail a ``"running"`` job when no runner in this process is actually working on it.

    Wall-clock staleness is the wrong test: a legitimately slow ``uv sync``
    can run far longer than any fixed timeout while staying healthy. The
    only honest signal is liveness: ``runner_alive`` is ``True`` for the
    whole duration :class:`~palmimo_portal.core.update_runner.UpdateRunner`
    is running a job in this process. A ``"running"`` job with no live
    runner behind it can only be a dead thread that failed to persist its
    own failure, which would otherwise block every future
    check/apply/rollback with 409 ``update_in_progress`` forever. Mirrors
    :func:`expire_stale_restart` (called at the top of every ``GET
    /update/status`` and before ``system/reboot``/``system/shutdown``
    refuse -- see :func:`current_update_state`); being a pure in-process
    liveness check rather than a wall-clock comparison, it is immune to a clock step.
    """
    if state.job.state != "running":
        return state
    if runner_alive:
        return state
    job = state.job
    new_job = UpdateJob(
        state="failed",
        kind=job.kind,
        target=job.target,
        step=job.step or "start",
        error=(
            "the update appears stuck (no update process is actually working on it) -- "
            "try again, or reboot from the Power screen if it keeps happening"
        ),
        started_at=job.started_at,
        finished_at=now,
    )
    return UpdateState(latest=state.latest, checked_at=state.checked_at, previous_tag=state.previous_tag, job=new_job)


def current_update_state(state_store: StateStore, now: float, runner_alive: bool) -> UpdateState:
    """Return the up-to-date, self-healed update state, persisting the result only if it changed.

    Applies :func:`expire_stale_restart` then :func:`expire_stale_running`
    to whatever is persisted, so ``system/reboot``/``system/shutdown``
    (``api/system.py``'s ``_refuse_while_updating``) sees the same
    decision a status poll would.
    """
    state = state_store.read_update_state()
    expired = expire_stale_restart(state, now)
    expired = expire_stale_running(expired, now, runner_alive)
    if expired is not state:
        state_store.write_update_state(expired)
    return expired
