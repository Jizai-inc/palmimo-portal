"""Display asset synchronization and pending retries."""

from __future__ import annotations

import logging
import threading

from palmimo_portal.ports import DisplaySyncPending, DisplaySyncResult, PlatformPort, RunLockTimeoutError, StateStore


logger = logging.getLogger("palmimo_portal")


def record_display_sync(state: StateStore, platform: PlatformPort, result: DisplaySyncResult) -> None:
    if result.result not in {"synced", "busy"}:
        return
    pending = DisplaySyncPending(True, result.reason, platform.read_boot_id()) if result.result == "busy" else None
    try:
        state.write_display_sync(pending)
    except OSError:
        logger.warning("display sync record could not be written")


class DisplaySyncService:
    def __init__(self, state: StateStore, platform: PlatformPort) -> None:
        self._state = state
        self._platform = platform
        self._running = threading.Event()

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def sync_locked(self, app_id: str | None = None) -> DisplaySyncResult:
        """The caller must hold run.lock."""
        self._running.set()
        try:
            result = self._platform.sync_display_assets(app_id)
            record_display_sync(self._state, self._platform, result)
            return result
        finally:
            self._running.clear()

    def sync(self, app_id: str | None = None) -> DisplaySyncResult:
        with self._state.lock_apps(), self._state.lock_run():
            return self.sync_locked(app_id)

    def retry_pending(self) -> None:
        try:
            with self._state.lock_run():
                pending = self._state.read_display_sync()
                if pending is not None and pending.pending:
                    self.sync_locked()
        except RunLockTimeoutError:
            return
