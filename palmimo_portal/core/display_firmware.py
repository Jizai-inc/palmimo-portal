"""Display firmware maintenance and recovery, under apps.lock then run.lock."""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import replace

from palmimo_portal.core.apps_jobs import read_manifest_for_app
from palmimo_portal.core.apps_start import RUNNING_ACTIVE_STATES, StartDeps, _start_app_locked
from palmimo_portal.ports import DisplayFirmwareResult, PlatformPort, PreemptedApp, RunLockTimeoutError


logger = logging.getLogger("palmimo_portal")


def _restore_locked(deps: StartDeps) -> None:
    record = deps.state.read_preempted_app()
    if record is None or record.by != "display-firmware":
        return
    name = record.preempted_app
    if name is not None:
        if deps.app_unit.status(name).active_state not in RUNNING_ACTIVE_STATES:
            _start_app_locked(replace(deps, platform_ready=lambda: True), name, host=socket.gethostname())
        logger.info("preempt: restored app=%s", name)
    deps.state.write_preempted_app(None)


def restore_preempted_app(deps: StartDeps) -> None:
    """Display maintenance recovery takes priority over autostart and does not require a network."""
    with deps.state.lock_apps(), deps.state.lock_run(timeout_s=3):
        record = deps.state.read_preempted_app()
        if record is None or record.by != "display-firmware":
            return
        try:
            _restore_locked(deps)
        except Exception:
            logger.exception("preempt: could not restore app; retaining record")


def run_display_firmware(deps: StartDeps, platform: PlatformPort) -> DisplayFirmwareResult:
    try:
        with deps.state.lock_run(timeout_s=3):
            preempted = False
            try:
                preemption = deps.state.read_preempted_app()
                if preemption is not None:
                    if preemption.by != "display-firmware":
                        return DisplayFirmwareResult(result="busy", reason="app recovery pending")
                    _restore_locked(deps)
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
