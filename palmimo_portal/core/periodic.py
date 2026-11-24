"""Periodic tasks use monotonic time; only network work requires NTP synchronization."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from palmimo_portal.core import apps_jobs
from palmimo_portal.core.apps import host_owner_from_url
from palmimo_portal.core.apps_jobs import AppsJobContext
from palmimo_portal.core.platform import PlatformLatestCache
from palmimo_portal.core.public_catalog import PublicCatalogCache, is_public_source
from palmimo_portal.ports import AppRecord, AppsLockTimeoutError, AppsState, ClockPort, GitCommandError, StateStore


logger = logging.getLogger("palmimo_portal")

#: How often every git-sourced, branch-pinned app is checked for an upstream update --
#: the same 5-minute interval design doc 3.2 gives `POST .../update/check`.
GIT_CHECK_INTERVAL_SECONDS = 300.0

#: How often the public catalog and the platform-bundle "latest release" cache refresh
#: (design doc 4.1/2.8's 1-hour cadence).
CATALOG_AND_PLATFORM_INTERVAL_SECONDS = 3600.0

#: How often PeriodicScheduler.run_forever wakes up to check whether a task is due --
#: independent of any task's own interval, just the granularity of "due" detection.
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class PeriodicTask:
    """Network tasks require synchronized time; local tasks can opt out."""

    name: str
    interval_seconds: float
    run: Callable[[], None]
    requires_ntp: bool = True


class PeriodicScheduler:
    """Intervals use monotonic time; triggers coalesce until the next eligible tick."""

    def __init__(self, clock: ClockPort, tasks: list[PeriodicTask]) -> None:
        self._clock = clock
        self._tasks = tasks
        start = clock.monotonic()
        self._last_run: dict[str, float] = {task.name: start for task in tasks}
        self._triggered: set[str] = set()
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._unsynced_streak = False
        self._stop = threading.Event()

    def trigger(self, name: str) -> None:
        with self._lock:
            self._triggered.add(name)

    def tick(self) -> None:
        now = self._clock.monotonic()
        synchronized = self._clock.ntp_synchronized()
        for task in self._tasks:
            with self._lock:
                if task.name in self._running:
                    continue
                if task.name not in self._triggered and now - self._last_run[task.name] < task.interval_seconds:
                    continue
                if task.requires_ntp and not synchronized:
                    if not self._unsynced_streak:
                        logger.info("catalog: skipped reason=clock_unsynced")
                        self._unsynced_streak = True
                    continue
                self._triggered.discard(task.name)
                self._last_run[task.name] = now
                self._running.add(task.name)
            try:
                task.run()
            except Exception:
                logger.exception("periodic: task %s failed", task.name)
            finally:
                with self._lock:
                    self._running.remove(task.name)
        if synchronized:
            self._unsynced_streak = False

    def run_forever(self, *, poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS) -> None:
        """Call :meth:`tick` every ``poll_interval_seconds`` until :meth:`stop` is called."""
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(poll_interval_seconds)

    def stop(self) -> None:
        self._stop.set()


def run_git_check_sweep(ctx: AppsJobContext, state_store: StateStore) -> None:
    """Check every branch-pinned git app for an upstream update, recording results.

    ``apps.lock`` is held only around the ledger read and the ledger
    write-back, never around the network calls in between -- an app job
    the operator is waiting on must be able to start and finish while this
    sweep's own ``git ls-remote``s are still in flight. Skips entirely
    (logged once) if the lock cannot be acquired for the initial read, and
    discards its results (logged once) if the lock cannot be acquired again
    for the write-back -- a job that started mid-sweep wins, and the next
    sweep picks the same apps back up.

    Skips a single app whose credential is marked
    :attr:`~palmimo_portal.ports.AppRecord.credential_rejected`; a 401/403
    from an app's check marks it (and every other app sharing the same
    ``host/owner`` credential) rejected instead of raising, so one bad
    credential does not abort the sweep for unrelated apps.
    """
    lock_cm = state_store.lock_apps()
    try:
        lock_cm.__enter__()
    except AppsLockTimeoutError:
        logger.info("apps: periodic check skipped reason=job_running")
        return
    try:
        state = state_store.read_apps_state()
    finally:
        lock_cm.__exit__(None, None, None)

    updates: dict[str, AppRecord] = {}
    rejected_host_owners: set[str] = set()
    for name, record in state.apps.items():
        if (
            is_public_source(record)
            or record.source.type != "git"
            or record.source.ref_kind != "branch"
            or record.credential_rejected
        ):
            continue
        try:
            available, remote_commit = apps_jobs.check_git_update(ctx, record)
        except GitCommandError as error:
            if error.status_code in (401, 403):
                assert record.source.url is not None
                host_owner = host_owner_from_url(record.source.url)
                logger.warning("git-credential: rejected host=%s status=%s", host_owner, error.status_code)
                # Only a *registered* credential can be "rejected" -- an app with no stored
                # credential got the 401/403 from the repo itself (private with nothing
                # configured, or a since-deleted credential), which is not something a
                # fresh PUT to git-credentials can ever clear.
                if host_owner is not None and ctx.secrets.get_git_credential(host_owner) is not None:
                    rejected_host_owners.add(host_owner)
                    ctx.secrets.mark_git_credential_rejected(host_owner, error.status_code)
                    updates[name] = replace(record, credential_rejected=True)
            else:
                logger.warning("apps: periodic check failed name=%s: %s", name, error)
            continue
        if available != record.update_available or remote_commit != record.latest_commit:
            updates[name] = replace(record, update_available=available, latest_commit=remote_commit)

    for name, record in state.apps.items():
        if is_public_source(record) or name in updates or record.credential_rejected or record.source.url is None:
            continue
        if host_owner_from_url(record.source.url) in rejected_host_owners:
            updates[name] = replace(record, credential_rejected=True)

    if not updates:
        return

    lock_cm = state_store.lock_apps()
    try:
        lock_cm.__enter__()
    except AppsLockTimeoutError:
        logger.info("apps: periodic check discarded reason=job_started_during_sweep")
        return
    try:

        def merge_updates(current: AppsState) -> AppsState:
            # Merge only the three sweep-derived fields into each freshly re-read record --
            # never replace it wholesale, or a concurrent write to the same app (e.g. PUT
            # .../autostart) landing while this sweep's network calls were in flight would be
            # lost. Skips an app no longer present in the ledger -- it may have been deleted
            # while this sweep's network calls were in flight.
            merged = dict(current.apps)
            for name, update in updates.items():
                current_record = merged.get(name)
                if current_record is None:
                    continue
                merged[name] = replace(
                    current_record,
                    update_available=update.update_available,
                    latest_commit=update.latest_commit,
                    credential_rejected=update.credential_rejected,
                )
            return replace(current, apps=merged)

        state_store.update_apps_state(merge_updates)
    finally:
        lock_cm.__exit__(None, None, None)


def build_scheduler(
    *,
    clock: ClockPort,
    apps_job_context: AppsJobContext,
    state_store: StateStore,
    platform_latest_cache: PlatformLatestCache,
    public_catalog_cache: PublicCatalogCache | None = None,
    display_sync: Callable[[], None] | None = None,
    display_recovery: Callable[[], None] | None = None,
) -> PeriodicScheduler:
    """Build the one scheduler ``api/app.py`` runs for the process's lifetime."""

    def _platform() -> None:
        platform_latest_cache.get(ntp_synchronized=True, force=True)

    tasks = [
        PeriodicTask(
            "git_check", GIT_CHECK_INTERVAL_SECONDS, lambda: run_git_check_sweep(apps_job_context, state_store)
        ),
        PeriodicTask("platform", CATALOG_AND_PLATFORM_INTERVAL_SECONDS, _platform),
    ]
    if public_catalog_cache is not None:
        tasks.append(
            PeriodicTask(
                "public_catalog",
                CATALOG_AND_PLATFORM_INTERVAL_SECONDS,
                lambda: public_catalog_cache.refresh(ntp_synchronized=True),
            )
        )
    if display_sync is not None:
        tasks.append(PeriodicTask("display_sync", 60.0, display_sync, requires_ntp=False))
    if display_recovery is not None:
        tasks.append(
            PeriodicTask("display_recovery", DEFAULT_POLL_INTERVAL_SECONDS, display_recovery, requires_ntp=False)
        )
    return PeriodicScheduler(clock, tasks)
