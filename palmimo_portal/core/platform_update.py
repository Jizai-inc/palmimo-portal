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

from palmimo_portal.ports import DisplayFirmwareResult, DisplayFirmwareState, PlatformJob, PlatformUpdateState


IDLE_PLATFORM_JOB = PlatformJob(
    state="idle", target_version=None, step=None, error=None, started_at=None, finished_at=None
)
IDLE_PLATFORM_STATE = PlatformUpdateState(job=IDLE_PLATFORM_JOB)

#: The only :class:`~palmimo_portal.ports.PlatformJobState` values a new update may start from.
_ALLOWS_NEW_JOB_STATES = frozenset({"idle", "done", "failed"})


class PlatformUpdateInProgressError(Exception):
    """Raised by :func:`start_update` when a platform job is already running."""


def start_update(state: PlatformUpdateState, target_version: int | None, now: float) -> PlatformUpdateState:
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
    return replace(state, job=replace(state.job, step=step))


def mark_done(state: PlatformUpdateState, now: float) -> PlatformUpdateState:
    """Firmware failure does not change platform readiness or job success."""
    return replace(state, job=replace(state.job, state="done", error=None, finished_at=now))


def mark_failed(state: PlatformUpdateState, step: str, error: str, now: float) -> PlatformUpdateState:
    """Record that the running job failed at ``step``."""
    return replace(state, job=replace(state.job, state="failed", step=step, error=error, finished_at=now))


def select_display_firmware_result(
    state: PlatformUpdateState,
    tool_state: DisplayFirmwareState | None,
    boot_id: str | None,
    *,
    fallback_result: DisplayFirmwareResult | None = None,
) -> DisplayFirmwareResult | None:
    """Boot identity orders results on devices whose wall clock can reset."""
    result = state.display_firmware
    if state.job.state == "running" or (boot_id and result is not None and result.boot_id == boot_id):
        return result
    return tool_state.as_result() if tool_state is not None else fallback_result or result


def finalize_after_restart(
    state: PlatformUpdateState,
    now: float,
    *,
    display_firmware: DisplayFirmwareState | None = None,
    boot_id: str | None = None,
) -> PlatformUpdateState:
    """Finish firmware stages using select_display_firmware_result; fail earlier interrupted stages."""
    if state.job.state != "running":
        return state
    if state.job.step == "display_firmware":
        terminal = mark_done(state, now)
        return replace(
            terminal,
            display_firmware=select_display_firmware_result(
                terminal,
                display_firmware,
                boot_id,
                fallback_result=DisplayFirmwareResult(
                    result="interrupted",
                    reason="Portal restarted during display firmware update",
                ),
            ),
        )
    job = state.job
    return replace(
        state,
        job=replace(
            job,
            state="failed",
            step=job.step or "fetch",
            error="interrupted: the Portal restarted before this job finished",
            finished_at=now,
        ),
    )
