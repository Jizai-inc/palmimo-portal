"""The periodic background scheduler: git update checks, catalog and platform-latest refresh.

Every task here is network work, so every task is gated on
:meth:`~palmimo_portal.ports.ClockPort.ntp_synchronized` (design doc 3.6): a
device without an RTC dials TLS to GitHub with a clock that can be years
off before its first sync, which fails outright. :class:`PeriodicScheduler`
is driven by :meth:`~palmimo_portal.ports.ClockPort.monotonic` only, never
wall-clock time -- an NTP sync jumping the wall clock must never make a task
fire early or late.

``api/app.py`` owns the daemon thread that calls :meth:`PeriodicScheduler.run_forever`;
tests drive :meth:`PeriodicScheduler.tick` directly instead, with a fake
clock, so no test ever sleeps for a real interval.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from palmimo_portal.core import apps_jobs
from palmimo_portal.core.apps import host_owner_from_url
from palmimo_portal.core.apps_jobs import AppsJobContext
from palmimo_portal.core.catalog import CatalogCache
from palmimo_portal.core.platform import PlatformLatestCache
from palmimo_portal.ports import AppRecord, AppsLockTimeoutError, ClockPort, GitCommandError, StateStore


logger = logging.getLogger("palmimo_portal")

#: How often every git-sourced, branch-pinned app is checked for an upstream update --
#: the same 5-minute interval design doc 3.2 gives `POST .../update/check`.
GIT_CHECK_INTERVAL_SECONDS = 300.0

#: How often the official catalog and the platform-bundle "latest release" cache refresh
#: (design doc 4.1/2.8's 1-hour cadence).
CATALOG_AND_PLATFORM_INTERVAL_SECONDS = 3600.0

#: How often PeriodicScheduler.run_forever wakes up to check whether a task is due --
#: independent of any task's own interval, just the granularity of "due" detection.
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class PeriodicTask:
    """One named unit of periodic network work, run no more often than ``interval_seconds`` apart."""

    name: str
    interval_seconds: float
    run: Callable[[], None]


class PeriodicScheduler:
    """Fires each :class:`PeriodicTask` at its own interval, measured on injected monotonic time.

    A task's first eligible firing is one full interval after the
    scheduler is constructed, not immediately -- every task here duplicates
    work a request path already does lazily on demand (``check_git_update``
    via ``POST .../update/check``, the catalog/platform caches via their
    own ``GET`` endpoints), so there is nothing useful to gain by racing
    that on startup, and every test that asserts "not before" gets a
    simple, uniform rule to check.
    """

    def __init__(self, clock: ClockPort, tasks: list[PeriodicTask]) -> None:
        self._clock = clock
        self._tasks = tasks
        start = clock.monotonic()
        self._last_run: dict[str, float] = {task.name: start for task in tasks}
        self._unsynced_streak = False
        self._stop = threading.Event()

    def tick(self) -> None:
        """Run every task whose interval has elapsed, skipping all of them if the clock is unsynchronized."""
        now = self._clock.monotonic()
        due = [task for task in self._tasks if now - self._last_run[task.name] >= task.interval_seconds]
        if not due:
            return
        if not self._clock.ntp_synchronized():
            if not self._unsynced_streak:
                logger.info("catalog: skipped reason=clock_unsynced")
                self._unsynced_streak = True
            return
        self._unsynced_streak = False
        for task in due:
            self._last_run[task.name] = now
            try:
                task.run()
            except Exception:
                logger.exception("periodic: task %s failed", task.name)

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
        if record.source.type != "git" or record.source.ref_kind != "branch" or record.credential_rejected:
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
        if name in updates or record.credential_rejected or record.source.url is None:
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
        current = state_store.read_apps_state()
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
        state_store.write_apps_state(replace(current, apps=merged))
    finally:
        lock_cm.__exit__(None, None, None)


def build_scheduler(
    *,
    clock: ClockPort,
    apps_job_context: AppsJobContext,
    state_store: StateStore,
    catalog_cache: CatalogCache,
    platform_latest_cache: PlatformLatestCache,
) -> PeriodicScheduler:
    """Build the one scheduler ``api/app.py`` runs for the process's lifetime."""

    def _catalog_and_platform() -> None:
        catalog_cache.refresh(ntp_synchronized=True)
        platform_latest_cache.get(ntp_synchronized=True, force=True)

    return PeriodicScheduler(
        clock,
        [
            PeriodicTask(
                "git_check", GIT_CHECK_INTERVAL_SECONDS, lambda: run_git_check_sweep(apps_job_context, state_store)
            ),
            PeriodicTask("catalog_and_platform", CATALOG_AND_PLATFORM_INTERVAL_SECONDS, _catalog_and_platform),
        ],
    )
