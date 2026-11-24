from __future__ import annotations

import json
import logging
import threading
from contextlib import ExitStack
from dataclasses import replace
from typing import Literal

from palmimo_portal.core.apps import new_job_id
from palmimo_portal.core.apps_start import (
    RUN_LOCK_WAIT_SECONDS,
    AppBusyError,
    AppJobInProgressStartError,
    AppStartError,
    StartDeps,
    _continue_start_locked,
    _plan_start,
    _release_start,
    app_unit_name,
)
from palmimo_portal.core.secrets import collect_registered_secret_values, mask_known_values
from palmimo_portal.ports import AppJob, AppsLockTimeoutError, AppsState, PolkitDeniedError, RunLockTimeoutError


logger = logging.getLogger("palmimo_portal")


class StartJobRunner:
    def __init__(self, deps: StartDeps, *, run_in_thread: bool = True) -> None:
        self._deps = deps
        self._run_in_thread = run_in_thread
        self._alive = threading.Event()

    def is_alive(self) -> bool:
        return self._alive.is_set()

    def start(self, name: str, *, host: str, origin: Literal["api", "autostart", "resume"]) -> AppJob:
        """Precheck and release synchronously, then hand apps.lock and run.lock to the worker."""
        deps = self._deps
        locks = ExitStack()
        marked_alive = False
        try:
            try:
                locks.enter_context(deps.state.lock_apps())
            except AppsLockTimeoutError:
                raise AppJobInProgressStartError() from None
            try:
                locks.enter_context(deps.state.lock_run(timeout_s=RUN_LOCK_WAIT_SECONDS))
            except RunLockTimeoutError:
                raise AppBusyError() from None
            plan = _plan_start(deps, name, host=host)
            _release_start(deps, plan, origin)
            platform = deps.apps_job_ctx.platform
            job = AppJob(
                new_job_id(),
                "start",
                "running",
                "firmware" if plan.display_stage else "launch",
                None,
                deps.now(),
                None,
                display_name=plan.record.name,
                boot_id=platform.read_boot_id() if platform else None,
            )
            self._alive.set()
            marked_alive = True
            deps.state.update_apps_state(lambda state: replace(state, current_job=job, current_job_app=name))
        except BaseException:
            if marked_alive:
                self._alive.clear()
            locks.close()
            raise
        result = job

        def advance(step: str) -> None:
            nonlocal result
            result = replace(result, step=step)
            deps.state.update_apps_state(lambda state: replace(state, current_job=result, current_job_app=name))

        def finish(terminal: AppJob) -> None:
            def update(state: AppsState) -> AppsState:
                record = state.apps.get(name)
                if record is None:
                    return replace(
                        state,
                        current_job=None,
                        current_job_app=None,
                        last_orphan_job=terminal,
                        last_orphan_job_app=name,
                    )
                return replace(
                    state,
                    apps={**state.apps, name: replace(record, last_job=terminal)},
                    current_job=None,
                    current_job_app=None,
                )

            deps.state.update_apps_state(update)

        def fail(error: Exception) -> AppJob:
            code = (
                error.code
                if isinstance(error, AppStartError)
                else "polkit_denied"
                if isinstance(error, PolkitDeniedError)
                else "start_failed"
            )
            try:
                detail = str(error)
                if isinstance(error, AppStartError):
                    detail += f": {json.dumps(error.params)}"
                elif not isinstance(error, PolkitDeniedError) and deps.apps_job_ctx.journal.can_read():
                    try:
                        page = deps.apps_job_ctx.journal.read(app_unit_name(name), cursor=None, lines=20)
                        detail += "\n" + "\n".join(entry.message for entry in page.entries)
                    except Exception:
                        logger.warning("app start journal could not be read app=%s", name)
                detail = mask_known_values(detail, collect_registered_secret_values(deps.apps_job_ctx.secrets))
            except Exception:
                logger.warning("app start diagnostic details unavailable app=%s", name)
                detail = code
            return replace(result, state="failed", error=detail, error_code=code, finished_at=deps.now())

        def run() -> None:
            nonlocal result
            try:
                try:
                    warning = _continue_start_locked(deps, plan, on_step=advance, origin=origin)
                    result = replace(result, state="done", step="launch", warning=warning, finished_at=deps.now())
                except Exception as error:
                    result = fail(error)
                try:
                    finish(result)
                except Exception:
                    logger.exception("app start terminal state could not be written app=%s", name)
            finally:
                self._alive.clear()
                locks.close()

        if self._run_in_thread:
            try:
                threading.Thread(target=run, daemon=True, name="palmimo-portal-app-start").start()
            except BaseException as error:
                try:
                    if isinstance(error, Exception):
                        finish(fail(error))
                finally:
                    self._alive.clear()
                    locks.close()
                raise
        else:
            run()
        return result
