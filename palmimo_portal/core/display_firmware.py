"""Display firmware maintenance and recovery, under apps.lock then run.lock."""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import replace
from typing import Literal

from palmimo_portal.core.apps_jobs import read_manifest_for_app
from palmimo_portal.core.apps_start import RUNNING_ACTIVE_STATES, StartDeps, _start_app_locked
from palmimo_portal.ports import DisplayFirmwareResult, PlatformPort, PreemptedApp, RunLockTimeoutError


logger = logging.getLogger("palmimo_portal")


def _restore_locked(deps: StartDeps) -> Literal["restored", "pending", "discarded"]:
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
            deadline = time.monotonic() + 20
            while active_state == "deactivating":
                if time.monotonic() >= deadline:
                    logger.warning("preempt: app=%s is still stopping; retaining record", name)
                    return "pending"
                time.sleep(0.1)
                active_state = deps.app_unit.status(name).active_state
        if active_state in {"inactive", "failed"}:
            _start_app_locked(
                replace(deps, platform_ready=lambda: True, before_start=lambda devices: None),
                name,
                host=socket.gethostname(),
            )
        elif active_state not in {"active", "activating"}:
            logger.warning("preempt: app=%s state=%s cannot be restored; retaining record", name, active_state)
            return "pending"
        logger.info("preempt: restored app=%s", name)
    deps.state.write_preempted_app(None)
    return "restored"


def restore_preempted_app(deps: StartDeps) -> bool:
    """Return True to suppress autostart after recovering or retaining an installed app."""
    with deps.state.lock_apps(), deps.state.lock_run(timeout_s=3):
        record = deps.state.read_preempted_app()
        if record is None or record.by != "display-firmware":
            return False
        try:
            return _restore_locked(deps) != "discarded"
        except Exception:
            logger.exception("preempt: could not restore app; retaining record")
            return True


def run_display_firmware(deps: StartDeps, platform: PlatformPort) -> DisplayFirmwareResult:
    try:
        with deps.state.lock_run(timeout_s=3):
            if deps.wifi_setup_active():
                return DisplayFirmwareResult(result="busy", reason="Wi-Fi setup is using the display")
            preempted = False
            try:
                preemption = deps.state.read_preempted_app()
                if preemption is not None:
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
                return platform.ensure_display_firmware()
            except Exception as error:
                logger.exception("display-firmware: maintenance failed")
                return DisplayFirmwareResult(result="failed", reason=str(error))
            finally:
                try:
                    if preempted:
                        _restore_locked(deps)
                except Exception:
                    logger.exception("display-firmware: could not restore app; retaining record")
    except RunLockTimeoutError:
        return DisplayFirmwareResult(result="busy", reason="another app operation is running")
    except Exception as error:
        logger.exception("display-firmware: could not acquire run lock")
        return DisplayFirmwareResult(result="failed", reason=str(error))
