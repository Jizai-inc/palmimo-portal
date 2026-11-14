"""Display maintenance; callers of run_display_firmware must hold apps.lock."""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import replace
from itertools import chain
from typing import Literal

from palmimo_portal.core.apps_jobs import read_manifest_for_app
from palmimo_portal.core.apps_start import (
    RUNNING_ACTIVE_STATES,
    AppStartError,
    StartDeps,
    _start_app_locked,
    autostart_apps,
)
from palmimo_portal.core.display_sync import record_display_sync
from palmimo_portal.ports import (
    AppsLockTimeoutError,
    DisplayFirmwareResult,
    DisplayProtocol,
    PlatformPort,
    PreemptedApp,
    RunLockTimeoutError,
)


logger = logging.getLogger("palmimo_portal")


def initial_display_protocol(deps: StartDeps) -> DisplayProtocol:
    state = deps.state.read_apps_state()
    preempt = deps.state.read_preempted_app()
    preempted = [preempt.preempted_app] if preempt is not None and preempt.preempted_app is not None else []
    running = (
        name
        for name, record in state.apps.items()
        if deps.app_unit.status(name).active_state in RUNNING_ACTIVE_STATES
        and "motor_display" in read_manifest_for_app(deps.apps_job_ctx, record).devices
    )
    candidates = chain(preempted, running, autostart_apps(state)[:1])
    for name in candidates:
        record = state.apps.get(name)
        if record is not None and record.display_firmware in {"1", "2"}:
            return 1 if record.display_firmware == "1" else 2
    return 2


def _restore_locked(deps: StartDeps, *, stop_wait_s: float = 20) -> Literal["restored", "pending", "discarded"]:
    record = deps.state.read_preempted_app()
    if record is None:
        return "discarded"
    if record.by != "display-firmware":
        return "pending"
    name = record.preempted_app
    if name is not None:
        if name not in deps.state.read_apps_state().apps:
            deps.state.write_preempted_app(None)
            logger.info("preempt: discarded record for removed app=%s", name)
            return "discarded"
        active_state = deps.app_unit.status(name).active_state
        if active_state == "deactivating":
            deadline = time.monotonic() + stop_wait_s
            while active_state == "deactivating":
                if time.monotonic() >= deadline:
                    logger.warning("preempt: app=%s is still stopping; retaining record", name)
                    return "pending"
                time.sleep(0.1)
                active_state = deps.app_unit.status(name).active_state
        if active_state in {"inactive", "failed"}:
            try:
                _start_app_locked(
                    replace(deps, platform_ready=lambda: True, before_start=lambda devices: None),
                    name,
                    host=socket.gethostname(),
                    origin="restore",
                )
            except AppStartError as error:
                if error.code in {"app_busy", "app_job_in_progress", "display_busy"}:
                    logger.warning("preempt: restore deferred app=%s code=%s", name, error.code)
                    return "pending"
                logger.warning("preempt: discarding unstartable app=%s code=%s", name, error.code)
                deps.run_dir.remove(name)
                deps.state.write_preempted_app(None)
                return "discarded"
        elif active_state not in {"active", "activating"}:
            logger.warning("preempt: app=%s state=%s cannot be restored; retaining record", name, active_state)
            return "pending"
        logger.info("preempt: restored app=%s", name)
    deps.state.write_preempted_app(None)
    return "restored"


def restore_preempted_app(deps: StartDeps, *, run_lock_timeout_s: float = 3, stop_wait_s: float = 20) -> bool:
    """Return True to suppress autostart after recovering or retaining an installed app."""
    with deps.state.lock_apps(), deps.state.lock_run(timeout_s=run_lock_timeout_s):
        record = deps.state.read_preempted_app()
        if record is None or record.by != "display-firmware":
            return False
        try:
            return _restore_locked(deps, stop_wait_s=stop_wait_s) != "discarded"
        except Exception:
            logger.exception("preempt: could not restore app; retaining record")
            return True


def retry_preempted_app(deps: StartDeps) -> None:
    record = deps.state.read_preempted_app()
    if record is None or record.by != "display-firmware":
        return
    try:
        restore_preempted_app(deps, run_lock_timeout_s=0, stop_wait_s=0)
    except (AppsLockTimeoutError, RunLockTimeoutError):
        return


def run_display_firmware(deps: StartDeps, platform: PlatformPort) -> DisplayFirmwareResult:
    try:
        with deps.state.lock_run(timeout_s=3):
            if deps.wifi_setup_active():
                return DisplayFirmwareResult(result="busy", reason="Wi-Fi setup is using the display")
            preempted = False
            hold_record = None
            try:
                initial = initial_display_protocol(deps)
                if platform.read_display_firmware_selection() is None:
                    platform.desire_display_firmware(initial, "initial")
                preemption = deps.state.read_preempted_app()
                if preemption is not None and preemption.by == "wifi-setup" and preemption.preempted_app is None:
                    hold_record = preemption
                elif preemption is not None:
                    if preemption.by != "display-firmware":
                        reason = (
                            "An app is stopped for Wi-Fi setup"
                            if preemption.by == "wifi-setup" and preemption.preempted_app is not None
                            else "Wi-Fi setup recovery is pending"
                        )
                        return DisplayFirmwareResult(result="busy", reason=reason)
                    if _restore_locked(deps) == "pending":
                        return DisplayFirmwareResult(result="busy", reason="app recovery pending")
                for name, record in deps.state.read_apps_state().apps.items():
                    if deps.app_unit.status(name).active_state not in RUNNING_ACTIVE_STATES:
                        continue
                    manifest = read_manifest_for_app(deps.apps_job_ctx, record)
                    if "motor_display" not in manifest.devices:
                        continue
                    deps.state.write_preempted_app(PreemptedApp(name, "display-firmware"))
                    preempted = True
                    logger.info("display-firmware: preempted app=%s", name)
                    deadline = time.monotonic() + 20
                    deps.app_unit.stop(name)
                    while deps.app_unit.status(name).active_state in RUNNING_ACTIVE_STATES:
                        if time.monotonic() >= deadline:
                            return DisplayFirmwareResult(result="busy", reason="app did not stop within 20 seconds")
                        time.sleep(0.1)
                    break
                result = platform.ensure_display_firmware()
                selection = platform.read_display_firmware_selection()
                if result.result in {"current", "updated"} and selection is not None and selection.protocol == 2:
                    try:
                        if deps.display_sync is not None:
                            sync = deps.display_sync.sync_locked()
                        else:
                            sync = platform.sync_display_assets(None)
                            record_display_sync(deps.state, platform, sync)
                        if sync.result == "failed":
                            logger.warning("display-firmware: asset synchronization failed reason=%s", sync.reason)
                    except Exception:
                        logger.exception("display-firmware: asset synchronization failed")
                return result
            except Exception as error:
                logger.exception("display-firmware: maintenance failed")
                return DisplayFirmwareResult(result="failed", reason=str(error))
            finally:
                try:
                    if preempted:
                        _restore_locked(deps)
                    if hold_record is not None and deps.state.read_preempted_app() is None:
                        deps.state.write_preempted_app(hold_record)
                except Exception:
                    logger.exception("display-firmware: could not restore app; retaining record")
    except RunLockTimeoutError:
        return DisplayFirmwareResult(result="busy", reason="another app operation is running")
    except Exception as error:
        logger.exception("display-firmware: could not acquire run lock")
        return DisplayFirmwareResult(result="failed", reason=str(error))
