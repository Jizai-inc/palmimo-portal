"""Runs install/update/delete jobs on a background thread, under ``apps.lock``, for the whole pipeline.

Mirrors :class:`~palmimo_portal.core.update_runner.UpdateRunner`'s shape
(``run_in_thread=False`` is the same synchronous test seam), but with one
twist install/update/delete do not need: :meth:`AppsJobRunner.start_install`
takes an already-fetched :class:`~palmimo_portal.core.apps_jobs.PreparedInstall`
(``api/apps.py`` calls :func:`~palmimo_portal.core.apps_jobs.prepare_install_zip`/
``prepare_install_git`` synchronously first) so the app's name -- and an
``app_exists`` conflict -- are known, and ``current_job_app`` can be set,
*before* this runner ever starts a thread. Only the unbounded step
(``uv sync``) actually runs in the background.

**Lock handoff across threads.** ``apps.lock`` is entered in the caller's
thread (a non-blocking attempt -- contention raises
:class:`~palmimo_portal.ports.AppsLockTimeoutError` immediately, before any
thread is spawned) and exited in the worker thread once the pipeline
finishes. This is safe for both backends: a ``flock`` is scoped to the open
file descriptor, not the thread that called ``flock()``, and
:class:`threading.Lock` (the fake's stand-in) explicitly allows release from
a different thread than acquired it. Held this way, the lock both serializes
app jobs and doubles as the mutual-exclusion signal a Portal self-update
probes (``api/update.py``).

**Progress persistence.** ``current_job``/``current_job_app`` are written to
``apps.json`` before the worker thread starts, and again on every
``on_step`` transition and at completion -- so a crash at any point leaves
exactly what :func:`~palmimo_portal.core.apps.finalize_apps_state` expects
to find and resolve into ``failed(interrupted)`` at the next startup.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from palmimo_portal.core import apps as apps_core
from palmimo_portal.core.apps_jobs import (
    AppsJobContext,
    PreparedInstall,
    commit_install,
    delete_from_ledger,
    purge_app_files,
    purge_path,
    update_git,
)
from palmimo_portal.core.apps_start import RUNNING_ACTIVE_STATES, AppRunningError
from palmimo_portal.core.secrets import mask_authorization_lines
from palmimo_portal.ports import AppExistsError, AppJob, AppNotFoundError, AppRecord, AppsState, AppUnitPort, StateStore


logger = logging.getLogger("palmimo_portal")


class AppsJobRunner:
    def __init__(
        self, state_store: StateStore, ctx: AppsJobContext, app_unit: AppUnitPort, *, run_in_thread: bool = True
    ) -> None:
        self._state = state_store
        self._ctx = ctx
        self._app_unit = app_unit
        self._run_in_thread = run_in_thread

    def _acquire_lock(self) -> contextlib.AbstractContextManager[None]:
        lock_cm = self._state.lock_apps()
        lock_cm.__enter__()
        return lock_cm

    def _spawn(self, run: Callable[[], None], lock_cm: contextlib.AbstractContextManager[None]) -> None:
        if not self._run_in_thread:
            run()
            return
        try:
            threading.Thread(target=run, daemon=True, name="palmimo-portal-apps-job").start()
        except BaseException:
            # `Thread.start()` can fail (e.g. `RuntimeError: can't start new thread`) before
            # `run()` ever gets a chance to release `apps.lock` itself -- caught as
            # `BaseException` so even a `SystemExit`/`KeyboardInterrupt` here still frees it.
            lock_cm.__exit__(None, None, None)
            raise

    def start_install(self, prepared: PreparedInstall) -> AppJob:
        """Start the slow half of an install (:func:`~palmimo_portal.core.apps_jobs.commit_install`).

        Raises:
            AppsLockTimeoutError: another app job is already in flight.
            AppExistsError: ``prepared.manifest.name`` is already installed
                (checked against a fresh read, immediately, before any
                thread is spawned).
        """
        lock_cm = self._acquire_lock()
        started = time.time()
        name = prepared.manifest.name
        try:
            state = self._state.read_apps_state()
            if name in state.apps:
                raise AppExistsError(name)
            job = AppJob(
                id=prepared.job_id,
                kind="install",
                state="running",
                step="fetch",
                error=None,
                started_at=started,
                finished_at=None,
            )
            self._state.write_apps_state(AppsState(apps=state.apps, current_job=job, current_job_app=name))
        except BaseException:
            lock_cm.__exit__(None, None, None)
            raise

        logger.info("apps: install started job_id=%s name=%s source=%s", prepared.job_id, name, prepared.source.type)
        result: dict[str, AppJob] = {}
        last_step = "fetch"

        def on_step(step: str) -> None:
            nonlocal last_step
            last_step = step
            logger.info("apps: job step job_id=%s name=%s step=%s", prepared.job_id, name, step)
            self._advance(name, step)

        def run() -> None:
            try:
                latest = self._state.read_apps_state()
                try:
                    _new_state, record = commit_install(self._ctx, latest, prepared, started, on_step)
                    self._write_own_record(name, record)
                    finished = record.last_job or job
                    result["job"] = finished
                    logger.info(
                        "apps: install finished job_id=%s name=%s lock_generated=%s duration_s=%.3f",
                        prepared.job_id,
                        name,
                        finished.lock_generated,
                        (finished.finished_at or time.time()) - started,
                    )
                except Exception as error:
                    logger.warning(
                        "apps: job failed job_id=%s step=%s stderr_tail=%s",
                        prepared.job_id,
                        last_step,
                        mask_authorization_lines(str(error)),
                    )
                    failed = self._fail_current_job(error, finished_at=time.time())
                    result["job"] = failed
            finally:
                self._safe_cleanup(prepared)
                lock_cm.__exit__(None, None, None)

        self._spawn(run, lock_cm)
        return result.get("job", job)

    def start_update(self, name: str) -> AppJob:
        """Start updating a git-sourced app.

        Raises:
            AppsLockTimeoutError: another app job is already in flight.
            AppNotFoundError: no app named ``name`` is installed.
        """
        lock_cm = self._acquire_lock()
        job_id = apps_core.new_job_id()
        started = time.time()
        try:
            state = self._state.read_apps_state()
            if name not in state.apps:
                raise AppNotFoundError(name)
            if self._app_unit.status(name).active_state in RUNNING_ACTIVE_STATES:
                raise AppRunningError(name)
            job = AppJob(
                id=job_id,
                kind="update",
                state="running",
                step="fetch",
                error=None,
                started_at=started,
                finished_at=None,
            )
            self._state.write_apps_state(AppsState(apps=state.apps, current_job=job, current_job_app=name))
        except BaseException:
            lock_cm.__exit__(None, None, None)
            raise

        result: dict[str, AppJob] = {}
        last_step = "fetch"

        def on_step(step: str) -> None:
            nonlocal last_step
            last_step = step
            logger.info("apps: job step job_id=%s name=%s step=%s", job_id, name, step)
            self._advance(name, step)

        def run() -> None:
            try:
                latest = self._state.read_apps_state()
                try:
                    _new_state, record = update_git(
                        self._ctx, latest, name, job_id=job_id, started=started, on_step=on_step
                    )
                    self._write_own_record(name, record, clear_credential_for_host_owner=True)
                    result["job"] = record.last_job or job
                except Exception as error:
                    logger.warning(
                        "apps: job failed job_id=%s step=%s stderr_tail=%s",
                        job_id,
                        last_step,
                        mask_authorization_lines(str(error)),
                    )
                    result["job"] = self._fail_current_job(error, finished_at=time.time(), attach_to=name)
            finally:
                lock_cm.__exit__(None, None, None)

        self._spawn(run, lock_cm)
        return result.get("job", job)

    def start_delete(self, name: str) -> AppJob:
        """Start deleting an app: drop it from the ledger, then remove its files (design doc 3.4 order).

        Raises:
            AppsLockTimeoutError: another app job is already in flight.
            AppNotFoundError: no app named ``name`` is installed.
        """
        lock_cm = self._acquire_lock()
        job_id = apps_core.new_job_id()
        started = time.time()
        try:
            state = self._state.read_apps_state()
            if name not in state.apps:
                raise AppNotFoundError(name)
            if self._app_unit.status(name).active_state in RUNNING_ACTIVE_STATES:
                raise AppRunningError(name)
            job = AppJob(
                id=job_id,
                kind="delete",
                state="running",
                step="unregister",
                error=None,
                started_at=started,
                finished_at=None,
            )
            self._state.write_apps_state(AppsState(apps=state.apps, current_job=job, current_job_app=name))
        except BaseException:
            lock_cm.__exit__(None, None, None)
            raise

        result: dict[str, AppJob] = {}

        def run() -> None:
            original_record = None
            try:
                current = self._state.read_apps_state()
                new_state, original_record = delete_from_ledger(current, name)
                self._state.write_apps_state(
                    AppsState(apps=new_state.apps, current_job=replace(job, step="cleanup"), current_job_app=name)
                )
                leftover = purge_app_files(self._ctx, name, on_step=lambda step: self._advance(name, step))
                final = self._state.read_apps_state()
                final_apps = {app_name: record for app_name, record in final.apps.items() if app_name != name}
                self._state.write_apps_state(AppsState(apps=final_apps, current_job=None, current_job_app=None))
                result["job"] = replace(
                    job,
                    state="done",
                    step="cleanup",
                    finished_at=time.time(),
                    error=(f"could not remove {leftover}" if leftover else None),
                )
            except Exception as error:
                logger.warning(
                    "apps: job failed job_id=%s step=cleanup stderr_tail=%s",
                    job_id,
                    mask_authorization_lines(str(error)),
                )
                failed = replace(job, state="failed", error=str(error), finished_at=time.time())
                final = self._state.read_apps_state()
                final_apps = dict(final.apps)
                if original_record is not None:
                    # The ledger entry (dropped by `delete_from_ledger` before file
                    # cleanup ran) must come back rather than vanish -- a delete
                    # that fails midway must leave a visible, retryable app, not
                    # a silent hole in the ledger.
                    final_apps[name] = replace(original_record, last_job=failed)
                self._state.write_apps_state(AppsState(apps=final_apps, current_job=None, current_job_app=None))
                result["job"] = failed
            finally:
                lock_cm.__exit__(None, None, None)

        self._spawn(run, lock_cm)
        return result.get("job", job)

    def _write_own_record(self, name: str, record: AppRecord, *, clear_credential_for_host_owner: bool = False) -> None:
        """Merge ``record`` into a freshly re-read ledger, rather than writing back the
        possibly-stale ``apps`` dict a job read before its unbounded step ran -- see the
        module docstring's lock-handoff note: a concurrent write to another app's record
        (e.g. ``PUT .../autostart``) landing while this job's ``uv sync`` was in flight
        must not be lost when this job completes.

        ``clear_credential_for_host_owner`` (set for a successful ``update``, design doc
        3.6) also un-marks ``credential_rejected`` on every other app sharing this app's
        git host/owner -- a completed clone/fetch proves the credential works again.
        """
        final = self._state.read_apps_state()
        apps = {**final.apps, name: record}
        if clear_credential_for_host_owner and record.source.url is not None:
            host_owner = apps_core.host_owner_from_url(record.source.url)
            if host_owner is not None:
                apps = apps_core.clear_credential_rejected(
                    AppsState(apps=apps, current_job=None, current_job_app=None), host_owner
                ).apps
        self._state.write_apps_state(AppsState(apps=apps, current_job=None, current_job_app=None))

    def _advance(self, name: str, step: str) -> None:
        current = self._state.read_apps_state()
        if current.current_job is not None and current.current_job_app == name:
            self._state.write_apps_state(
                AppsState(apps=current.apps, current_job=replace(current.current_job, step=step), current_job_app=name)
            )

    def _fail_current_job(self, error: Exception, *, finished_at: float, attach_to: str | None = None) -> AppJob:
        current = self._state.read_apps_state()
        job = current.current_job
        assert job is not None
        failed = replace(job, state="failed", error=str(error), finished_at=finished_at)
        apps = dict(current.apps)
        if attach_to is not None and attach_to in apps:
            apps[attach_to] = replace(apps[attach_to], last_job=failed)
        self._state.write_apps_state(AppsState(apps=apps, current_job=None, current_job_app=None))
        return failed

    def _safe_cleanup(self, prepared: PreparedInstall) -> None:
        purge_path(self._ctx, prepared.staging_container)
