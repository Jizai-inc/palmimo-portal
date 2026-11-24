"""Display asset synchronization and pending retries."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from palmimo_portal.ports import (
    DisplaySyncPending,
    DisplaySyncResult,
    DisplayToolError,
    PlatformCommandError,
    PlatformPort,
    RunLockTimeoutError,
    StateStore,
)


logger = logging.getLogger("palmimo_portal")


def record_display_sync(state: StateStore, platform: PlatformPort, result: DisplaySyncResult) -> None:
    if result.result == "synced":
        pending = None
    elif result.result == "busy" or (result.result == "failed" and result.deleted):
        pending = DisplaySyncPending(True, result.reason, platform.read_boot_id())
    else:
        return
    _write_display_sync(state, pending)


def _write_display_sync(state: StateStore, pending: DisplaySyncPending | None) -> None:
    try:
        state.write_display_sync(pending)
    except OSError:
        logger.warning("display sync record could not be written")


class DisplaySyncService:
    def __init__(
        self,
        state: StateStore,
        platform: PlatformPort,
        *,
        platform_ready: Callable[[], bool] = lambda: True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state = state
        self._platform = platform
        self._running = threading.Event()
        self._platform_ready = platform_ready
        self._monotonic = monotonic
        self._retry_reason: str | None = None
        self._retry_delay = 60.0
        self._retry_at = 0.0

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def sync_locked(self, app_id: str | None = None) -> DisplaySyncResult:
        """The caller must hold run.lock."""
        self._running.set()
        try:
            try:
                result = self._platform.sync_display_assets(app_id)
            except (PlatformCommandError, DisplayToolError) as error:
                _write_display_sync(self._state, DisplaySyncPending(True, str(error), self._platform.read_boot_id()))
                raise
            record_display_sync(self._state, self._platform, result)
            if result.result == "synced":
                self._reset_retry()
            return result
        finally:
            self._running.clear()

    def sync(self, app_id: str | None = None) -> DisplaySyncResult:
        with self._state.lock_apps(), self._state.lock_run():
            return self.sync_locked(app_id)

    def retry_pending(self) -> None:
        try:
            if not self._platform_ready():
                self._reset_retry()
                return
            with self._state.lock_run():
                pending = self._state.read_display_sync()
                if pending is None or not pending.pending:
                    self._reset_retry()
                    return
                if self._monotonic() < self._retry_at:
                    return
                try:
                    result = self.sync_locked()
                except (PlatformCommandError, DisplayToolError) as error:
                    self._retry_failed(str(error))
                else:
                    if result.result == "failed":
                        self._retry_failed(result.reason or "failed")
                    else:
                        self._reset_retry()
        except RunLockTimeoutError:
            return

    def _reset_retry(self) -> None:
        self._retry_reason = None
        self._retry_delay = 60.0
        self._retry_at = 0.0

    def _retry_failed(self, reason: str) -> None:
        if reason != self._retry_reason:
            logger.warning("display sync retry failed reason=%s", " ".join(reason.splitlines()))
            self._retry_delay = 60.0
        self._retry_reason = reason
        self._retry_delay = min(self._retry_delay * 2, 900.0)
        self._retry_at = self._monotonic() + self._retry_delay
