"""Drives one :class:`~palmimo_portal.ports.Updater` job (apply then restart) to completion.

:class:`UpdateRunner` is the glue between the pure transitions in
:mod:`palmimo_portal.core.update` and the two side-effecting ports this
feature needs: :class:`~palmimo_portal.ports.Updater` (``git fetch`` ->
``git checkout`` -> ``uv sync``) and :class:`~palmimo_portal.ports.SystemPort`
(``restart_portal``). Kept in ``core/`` (not ``api/``) since none of this
needs FastAPI -- ``api/update.py`` only constructs one and calls
:meth:`UpdateRunner.start`.

Persists :class:`~palmimo_portal.ports.UpdateJob` progress at every step
through :class:`~palmimo_portal.ports.StateStore`, so ``GET /update/status``
always reflects the runner's current step even though the runner does the
work off the request/response cycle.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from palmimo_portal.core.update import advance, mark_failed, mark_restarting
from palmimo_portal.ports import AdapterUnavailableError, StateStore, SystemPort, Updater, UpdateStepError


logger = logging.getLogger("palmimo_portal")


class UpdateRunner:
    """Runs one update/rollback job: :meth:`Updater.apply` then :meth:`SystemPort.restart_portal`.

    The caller (``api/update.py``) is responsible for the one-job-at-a-time
    guarantee at the :class:`~palmimo_portal.ports.UpdateState` level,
    transitioning the persisted job from idle/done/failed to running under
    its own lock *before* constructing or starting a runner (see
    :func:`~palmimo_portal.core.update.start_apply`'s 409 rule). This
    class's own :attr:`_busy_lock` is a second, narrower guard against the
    *same instance* running two jobs concurrently if :meth:`start` is
    somehow called twice.

    ``run_in_thread`` (default ``True``) spawns a daemon thread so
    ``POST /update/apply``/``POST /update/rollback`` can return 202
    immediately; ``False`` runs the whole job inline, letting a test drive
    the fake updater synchronously.

    ``restart_delay_seconds`` (default ``1.0``) is slept between
    persisting ``"restarting"`` and calling
    :meth:`SystemPort.restart_portal`, so a fast apply cannot have systemd
    tear this process down before the triggering request's 202 response
    finishes writing to the socket. Tests pass ``0`` to skip the delay.

    ``alive`` (default ``None``), when given, is set for the whole
    duration of :meth:`_run_locked` and cleared otherwise -- the liveness
    signal :func:`~palmimo_portal.core.update.expire_stale_running` uses
    instead of a wall-clock timeout, since a ``"running"`` job with this
    flag clear can only be a dead thread. ``api/app.py`` constructs one
    :class:`~threading.Event` per app alongside the one
    :class:`UpdateRunner`, both stored on ``app.state``.
    """

    def __init__(
        self,
        state: StateStore,
        updater: Updater,
        system: SystemPort,
        *,
        run_in_thread: bool = True,
        restart_delay_seconds: float = 1.0,
        alive: threading.Event | None = None,
    ) -> None:
        self._state = state
        self._updater = updater
        self._system = system
        self._run_in_thread = run_in_thread
        self._restart_delay_seconds = restart_delay_seconds
        self._alive = alive
        self._busy_lock = threading.Lock()

    def start(self, target: str) -> None:
        """Start applying *target*, in a background thread unless ``run_in_thread`` is ``False``."""
        if self._run_in_thread:
            thread = threading.Thread(target=self._run, args=(target,), daemon=True, name="palmimo-portal-update")
            thread.start()
        else:
            self._run(target)

    def _run(self, target: str) -> None:
        if not self._busy_lock.acquire(blocking=False):
            logger.warning("update runner asked to start %r while already busy -- ignoring", target)
            return
        try:
            self._run_locked(target)
        finally:
            self._busy_lock.release()

    def _run_locked(self, target: str) -> None:
        def on_step(step: str) -> None:
            logger.info("update: %s %s", step, target)
            self._state.write_update_state(advance(self._state.read_update_state(), step))

        if self._alive is not None:
            self._alive.set()
        try:
            self._run_steps(target, on_step)
        except Exception as error:
            # Anything not already handled by the narrower except clauses
            # below must never leave the job wedged in "running". `job.step
            # or "start"` mirrors finalize_after_restart's own fallback.
            logger.error("update: unexpected error while applying %s: %s", target, error, exc_info=True)
            try:
                state = self._state.read_update_state()
                step = state.job.step or "start"
                self._state.write_update_state(mark_failed(state, step, f"unexpected error: {error!r}", time.time()))
            except Exception:
                # Persisting the failure also failed (e.g. a full disk) --
                # log with a traceback so an operator reading journalctl can
                # see the job is stuck in "running" and why. See
                # GET /update/status's expire_stale_running for how it
                # eventually gets unstuck without a reboot.
                logger.exception("update: failed to persist the failure state for %s after the error above", target)
        finally:
            if self._alive is not None:
                self._alive.clear()

    def _run_steps(self, target: str, on_step: Callable[[str], None]) -> None:
        try:
            self._updater.apply(target, on_step)
        except UpdateStepError as error:
            logger.warning("update: %s failed for %s: %s", error.step, target, error)
            self._state.write_update_state(
                mark_failed(self._state.read_update_state(), error.step, str(error), time.time())
            )
            return

        self._state.write_update_state(mark_restarting(self._state.read_update_state(), time.time()))
        # Give the 202 response that triggered this job time to flush to
        # the client's socket before asking systemd to kill and restart
        # this process (see the class docstring's `restart_delay_seconds`).
        time.sleep(self._restart_delay_seconds)
        try:
            self._system.restart_portal()
        except AdapterUnavailableError as error:
            logger.warning("update: restart_portal failed after applying %s: %s", target, error)
            message = f"{error} -- a manual reboot from the Power screen will finish the update"
            self._state.write_update_state(
                mark_failed(self._state.read_update_state(), "restart", message, time.time())
            )
        except Exception as error:
            # Anything other than AdapterUnavailableError must still be
            # attributed to the "restart" step, not fall through to
            # _run_locked's generic handler, which has no idea a restart
            # was attempted and would misattribute the failure to whatever
            # step `job.step` last held.
            logger.error(
                "update: unexpected error from restart_portal after applying %s: %s", target, error, exc_info=True
            )
            message = f"unexpected error: {error!r} -- a manual reboot from the Power screen will finish the update"
            self._state.write_update_state(
                mark_failed(self._state.read_update_state(), "restart", message, time.time())
            )
