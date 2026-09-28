"""Pure state-transition rules for one platform-bundle update job (design doc 2.8).

Mirrors :mod:`palmimo_portal.core.update`'s shape (idle -> running ->
done/failed, persisted via :class:`~palmimo_portal.ports.StateStore`,
finalized at startup) but without the "checking"/"restarting" states that
module needs: a platform job's success is decided entirely by its own
``record`` step, before ``SystemPort.restart_portal`` is ever called (see
:mod:`palmimo_portal.core.platform`'s module docstring), so there is no
window where this process must come back from a restart to learn whether
its own job succeeded.
"""

from __future__ import annotations

from dataclasses import replace

from palmimo_portal.ports import PlatformJob, PlatformUpdateState


IDLE_PLATFORM_JOB = PlatformJob(
    state="idle", target_version=None, step=None, error=None, started_at=None, finished_at=None
)
IDLE_PLATFORM_STATE = PlatformUpdateState(job=IDLE_PLATFORM_JOB)

#: The only :class:`~palmimo_portal.ports.PlatformJobState` values a new update may start from.
_ALLOWS_NEW_JOB_STATES = frozenset({"idle", "done", "failed"})


class PlatformUpdateInProgressError(Exception):
    """Raised by :func:`start_update` when a platform job is already running."""


def start_update(state: PlatformUpdateState, target_version: int, now: float) -> PlatformUpdateState:
    """Begin applying ``target_version``, or raise if a job is already in flight.

    Raises:
        PlatformUpdateInProgressError: ``state.job.state`` is ``"running"``.
    """
    if state.job.state not in _ALLOWS_NEW_JOB_STATES:
        raise PlatformUpdateInProgressError()
    return PlatformUpdateState(
        job=PlatformJob(
            state="running", target_version=target_version, step=None, error=None, started_at=now, finished_at=None
        )
    )


def advance(state: PlatformUpdateState, step: str) -> PlatformUpdateState:
    """Record that the running job has reached ``step``."""
    return PlatformUpdateState(job=replace(state.job, step=step))


def mark_done(state: PlatformUpdateState, now: float) -> PlatformUpdateState:
    """Record that the running job's ``record`` step succeeded."""
    return PlatformUpdateState(job=replace(state.job, state="done", error=None, finished_at=now))


def mark_failed(state: PlatformUpdateState, step: str, error: str, now: float) -> PlatformUpdateState:
    """Record that the running job failed at ``step``."""
    return PlatformUpdateState(job=replace(state.job, state="failed", step=step, error=error, finished_at=now))


def finalize_after_restart(state: PlatformUpdateState, now: float) -> PlatformUpdateState:
    """Fail a ``"running"`` job left over from a process that died before finishing it.

    Called once at Portal startup. A job in ``"idle"``/``"done"``/``"failed"``
    is returned unchanged.
    """
    if state.job.state != "running":
        return state
    job = state.job
    return PlatformUpdateState(
        job=replace(
            job,
            state="failed",
            step=job.step or "fetch",
            error="interrupted: the Portal restarted before this job finished",
            finished_at=now,
        )
    )
