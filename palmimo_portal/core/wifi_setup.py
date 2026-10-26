from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from palmimo_portal.core.apps_jobs import read_manifest_for_app
from palmimo_portal.core.apps_start import (
    RUNNING_ACTIVE_STATES,
    AppBusyError,
    AppStartError,
    StartDeps,
    _start_app_locked,
    _stop_app_locked,
)
from palmimo_portal.core.wifi_attempt_manager import AttemptInProgressError, WifiAttemptManager
from palmimo_portal.core.wifi_qr import Rejection, parse_wifi_qr
from palmimo_portal.ports import (
    AdapterUnavailableError,
    AppsLockTimeoutError,
    ConnectionState,
    NetworkPort,
    PolkitDeniedError,
    PreemptedApp,
    RunLockTimeoutError,
    WifiProfilePort,
    WifiQrClientPort,
    WifiQrUnitMissingError,
    WifiQrUnitPort,
    WifiQrUnitStatus,
)


logger = logging.getLogger("palmimo_portal")
SETUP_DEVICES = frozenset({"camera", "motor_display"})


class WifiSetupApps:
    def __init__(self, deps: StartDeps, *, hostname: str = "palmimo") -> None:
        self.deps = deps
        self.hostname = hostname
        self._prepare_warning = False

    def conflicting_apps(self) -> list[str]:
        apps = self.deps.state.read_apps_state().apps
        conflicts = []
        for name in self.deps.app_unit.list_active_app_units():
            if self.deps.app_unit.status(name).active_state not in {"active", "activating"}:
                continue
            if name not in apps:
                raise ValueError("Running app has no device declaration")
            manifest = read_manifest_for_app(self.deps.apps_job_ctx, apps[name])
            if SETUP_DEVICES.intersection(manifest.devices):
                conflicts.append(name)
        return conflicts

    def prepare_locked(self) -> bool:
        """Caller holds run.lock through reader StartUnit."""
        try:
            record = self.deps.state.read_preempted_app()
            if record is not None and record.by != "wifi-setup":
                return False
            conflicts = self.conflicting_apps()
            if record is not None and record.hold and conflicts:
                return False
            if conflicts:
                if len(conflicts) != 1:
                    raise ValueError("Multiple apps occupy setup devices")
                name = conflicts[0]
                self.deps.state.write_preempted_app(PreemptedApp(name, "wifi-setup"))
                try:
                    _stop_app_locked(self.deps, name)
                except Exception:
                    logger.warning("wifi-setup: app stop failed app=%s", name)
                logger.info("wifi-setup: preempted app=%s", name)
        except Exception:
            if not self._prepare_warning:
                logger.warning("wifi-setup: cannot safely preempt app")
                self._prepare_warning = True
            return False
        self._prepare_warning = False
        return True

    def hold_locked(self) -> None:
        record = self.deps.state.read_preempted_app()
        if record is not None and record.by != "wifi-setup":
            raise ValueError("App recovery belongs to another operation")
        self.deps.state.write_preempted_app(PreemptedApp(None, "wifi-setup", hold=True))
        logger.info("wifi-setup: hold reason=app_start")

    def clear_hold(self) -> None:
        with self.deps.state.lock_run():
            record = self.deps.state.read_preempted_app()
            if record is not None and record.by == "wifi-setup" and record.hold:
                self.deps.state.write_preempted_app(
                    replace(record, hold=False) if record.preempted_app is not None else None
                )

    def restore(self, *, hold: bool) -> bool:
        try:
            with self.deps.state.lock_apps(), self.deps.state.lock_run():
                record = self.deps.state.read_preempted_app()
                if record is None or record.by != "wifi-setup" or record.preempted_app is None:
                    return True
                name = record.preempted_app
                result = "already_running"
                try:
                    if self.deps.app_unit.status(name).active_state not in RUNNING_ACTIVE_STATES:
                        _start_app_locked(
                            replace(self.deps, before_start=lambda devices: None), name, host=self.hostname
                        )
                        result = "started"
                except AppStartError as error:
                    if error.code in {"app_busy", "app_job_in_progress"}:
                        return False
                    result = "failed"
                    logger.warning("wifi-setup: cannot restore app=%s", name)
                except Exception:
                    result = "failed"
                    logger.warning("wifi-setup: cannot restore app=%s", name)
                self.deps.state.write_preempted_app(PreemptedApp(None, "wifi-setup", hold=True) if hold else None)
                logger.info("wifi-setup: restored app=%s result=%s", name, result)
                if hold:
                    logger.info("wifi-setup: hold reason=reader_unavailable")
                return True
        except (AppsLockTimeoutError, RunLockTimeoutError):
            return False


@dataclass(frozen=True)
class WifiSetupStatus:
    mode: str
    screen: str | None


@dataclass(frozen=True)
class Pending:
    attempt_id: str | None
    accepted_at: float


class WifiSetup:
    """Never hold the setup condition across D-Bus calls, trial operations, or socket close."""

    APP_STOP_TIMEOUT = 10.0

    def __init__(
        self,
        *,
        network: NetworkPort,
        profiles: WifiProfilePort,
        attempts: WifiAttemptManager,
        unit: WifiQrUnitPort,
        apps: WifiSetupApps,
        client_factory: Callable[[Callable[[dict[str, Any]], None], Callable[[], None]], WifiQrClientPort],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        hostname: str,
    ) -> None:
        self.network = network
        self.profiles = profiles
        self.attempts = attempts
        self.unit = unit
        self.apps = apps
        self._factory = client_factory
        self._now = monotonic
        self._sleep = sleep
        self._hostname = hostname
        self._condition = threading.Condition()
        self._mode = "idle"
        self._screen: str | None = None
        self._screen_at = 0.0
        self._started_at = 0.0
        self._retry_at: float | None = None
        self._retry_delay = 30.0
        self._pending: Pending | None = None
        self._ap_elapsed = 0.0
        self._last_poll: float | None = None
        self._was_ap = False
        self._poll_warning = False
        self._client: WifiQrClientPort | None = None
        self._socket_connected = False
        self._disconnected = False
        self._generation = 0
        self._restore_hold: bool | None = None
        self._shutdown = threading.Event()

    def status(self) -> WifiSetupStatus:
        with self._condition:
            return WifiSetupStatus(self._mode, self._screen)

    def _show_locked(self, state: str, **fields: str) -> None:
        self._screen = state
        self._screen_at = self._now()
        if self._client is not None:
            try:
                if not self._client.send({"type": "screen", "state": state, **fields}):
                    self._disconnected = True
            except Exception:
                self._disconnected = True
                logger.warning("wifi-setup: screen send failed")

    def on_message(self, message: dict[str, Any], *, generation: int | None = None) -> None:
        with self._condition:
            if generation is not None and generation != self._generation:
                return
            if message.get("type") == "hello" and self._mode == "starting":
                self._mode = "active"
                self._retry_delay = 30
                self._show_locked("scanning")
                return
            if self._mode != "active" or self._screen != "scanning" or message.get("type") != "qr":
                return
            text = message.get("text")
            if not isinstance(text, str):
                return
            self._show_locked("checking")
            credential = parse_wifi_qr(text)
            if isinstance(credential, Rejection):
                self._show_locked("rejected", reason=credential.reason)
                return
            generation = self._generation
            self._pending = Pending(None, self._now())
            self._show_locked("connecting", ssid=credential.ssid)
        logger.info("wifi qr accepted ssid=%s", credential.ssid)
        try:
            attempt_id = self.attempts.start_wifi_attempt(
                "credentials", credential.ssid.encode("utf-8"), credential.password
            )
        except Exception as error:
            busy = isinstance(error, AttemptInProgressError)
            with self._condition:
                if self._generation == generation and self._screen == "connecting":
                    self._pending = None
                    self._show_locked("rejected" if busy else "failed", reason="busy" if busy else "connect_failed")
            if not busy:
                logger.warning("wifi-setup: Wi-Fi attempt could not start")
            return
        with self._condition:
            if self._generation == generation and self._pending is not None:
                self._pending = replace(self._pending, attempt_id=attempt_id)

    def _socket_lost(self, generation: int) -> None:
        with self._condition:
            if generation == self._generation and self._mode in {"starting", "active"}:
                self._disconnected = True

    def _connect_socket(self) -> None:
        with self._condition:
            if self._mode != "starting" or self._socket_connected:
                return
            generation = self._generation

        def message(value: dict[str, Any]) -> None:
            self.on_message(value, generation=generation)

        client = self._factory(message, lambda: self._socket_lost(generation))
        with self._condition:
            if self._mode != "starting" or generation != self._generation:
                return
            self._client = client
        try:
            client.connect()
        except Exception:
            client.close()
            return
        with self._condition:
            stale = generation != self._generation or self._mode not in {"starting", "active"}
            if not stale:
                self._socket_connected = True
        if stale:
            client.close()

    def _enter(self) -> None:
        try:
            with self.apps.deps.state.lock_run():
                status = self.unit.status()
                if status.load_state == "not-found":
                    raise WifiQrUnitMissingError
                with self._condition:
                    if self._mode != "idle" or self._shutdown.is_set():
                        return
                    self._generation += 1
                    generation = self._generation
                    self._mode = "starting"
                    self._started_at = self._now()
                    self._disconnected = False
                if not self.apps.prepare_locked():
                    with self._condition:
                        if self._generation == generation:
                            self._mode = "idle"
                            self._condition.notify_all()
                    return
                with self._condition:
                    if self._generation != generation:
                        return
                self.unit.start()
                with self._condition:
                    stale = self._generation != generation or self._shutdown.is_set()
                if stale:
                    self.unit.stop()
                    self._wait_stopped(6)
                    return
        except RunLockTimeoutError:
            return
        except WifiQrUnitMissingError:
            with self._condition:
                self._mode = "unavailable"
                self._retry_at = None
                self._restore_hold = True
            return
        except Exception as error:
            if isinstance(error, PolkitDeniedError):
                logger.error("wifi-setup: reader control denied")
            else:
                logger.warning("wifi-setup: reader start failed")
            self.stop("reader", fault=True)
            return
        self._connect_socket()

    def _wait_stopped(self, timeout: float) -> None:
        deadline = self._now() + timeout
        while self.unit.status().active_state not in {"inactive", "failed"}:
            if self._now() >= deadline:
                raise TimeoutError("Wi-Fi QR reader did not stop")
            self._sleep(min(0.1, deadline - self._now()))

    def startup(self) -> None:
        try:
            self.unit.stop()
            self._wait_stopped(6)
        except WifiQrUnitMissingError:
            with self._condition:
                self._mode = "unavailable"
        except PolkitDeniedError:
            logger.error("wifi-setup: startup reader stop denied")
        except Exception:
            logger.warning("wifi-setup: startup reader stop failed")

    def stop(self, reason: str, *, fault: bool = False, result: WifiQrUnitStatus | None = None) -> None:
        with self._condition:
            if self._mode == "stopping":
                if not self._condition.wait_for(lambda: self._mode != "stopping", timeout=10):
                    raise AppBusyError
                return
            if self._mode in {"idle", "backoff", "unavailable"} and reason != "reader":
                return
            self._mode = "stopping"
            self._generation += 1
            client, self._client = self._client, None
            self._socket_connected = False
            self._pending = None
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning("wifi-setup: socket close failed")
        try:
            if result is None:
                result = self.unit.status()
            self.unit.stop()
            self._wait_stopped(6)
        except Exception:
            logger.warning("wifi-setup: reader stop failed")
        with self._condition:
            self._screen = None
            self._mode = "idle"
            self._retry_at = None
            if reason == "reader":
                self._restore_hold = True
                self._mode = "backoff"
                delay = self._retry_delay
                code = (
                    result.exec_main_status
                    if result is not None
                    and result.exec_main_code == 1
                    and result.result == "exit-code"
                    and result.active_state in {"inactive", "failed"}
                    else None
                )
                if code in {69, 78}:
                    self._mode = "unavailable"
                    self._retry_at = self._now() + 3600 if code == 78 else None
                elif code == 75:
                    self._retry_at = self._now() + 30
                elif (
                    not fault
                    and result is not None
                    and result.result == "success"
                    and result.active_state == "inactive"
                    and result.exec_main_status == 0
                ):
                    self._mode = "idle"
                else:
                    self._retry_at = self._now() + delay
                    self._retry_delay = min(delay * 2, 600)
            elif reason == "connected":
                self._restore_hold = False
            self._condition.notify_all()

    def before_app_start(self, devices: list[str]) -> None:
        if not SETUP_DEVICES.intersection(devices):
            return
        with self._condition:
            needs_stop = self._mode in {"starting", "active", "stopping"}
        if needs_stop:
            completed = threading.Event()
            failed = threading.Event()

            def stop() -> None:
                try:
                    self.stop("app")
                except Exception:
                    failed.set()
                finally:
                    completed.set()

            threading.Thread(target=stop, daemon=True, name="wifi-setup-app-stop").start()
            if not completed.wait(self.APP_STOP_TIMEOUT) or failed.is_set():
                raise AppBusyError
        self.apps.hold_locked()

    def _restore(self) -> None:
        with self._condition:
            hold = self._restore_hold
        if hold is not None and self.apps.restore(hold=hold):
            with self._condition:
                if self._restore_hold == hold:
                    self._restore_hold = None

    def tick(self) -> None:
        self._restore()
        now = self._now()
        with self._condition:
            elapsed = min(2.0, max(0.0, now - self._last_poll)) if self._last_poll is not None else 0.0
            self._last_poll = now
        try:
            network = self.network.get_status()
            known = self.network.has_known_networks()
            lan = (
                network.state is ConnectionState.CONNECTED
                and bool(network.ip_address)
                and self.profiles.device_status().state == 100
            )
        except AdapterUnavailableError:
            if not self._poll_warning:
                logger.warning("wifi-setup: network observation unavailable")
                self._poll_warning = True
            return
        self._poll_warning = False
        if lan:
            with suppress(RunLockTimeoutError):
                self.apps.clear_hold()
        with self._condition:
            ap = network.state is ConnectionState.UNPROVISIONED
            self._ap_elapsed = self._ap_elapsed + elapsed if ap and self._was_ap else 0
            self._was_ap = ap
            if self._mode in {"backoff", "unavailable"} and self._retry_at is not None and now >= self._retry_at:
                self._mode = "idle"
            mode = self._mode
            pending = self._pending
        if mode == "idle":
            if lan:
                self.apps.restore(hold=False)
            elif ap and self._ap_elapsed >= (60 if known else 5) and not self._shutdown.is_set():
                self._enter()
            return
        if mode not in {"starting", "active"}:
            return
        try:
            result = self.unit.status()
        except WifiQrUnitMissingError:
            self.stop("reader", result=WifiQrUnitStatus("failed", "not-found", "exit-code", 1, 69))
            return
        if result.active_state in {"inactive", "failed"}:
            self.stop("reader", result=result)
            return
        with self._condition:
            disconnected = self._disconnected
        if disconnected:
            self.stop("reader", result=result)
            return
        if mode == "starting":
            self._connect_socket()
            with self._condition:
                timed_out = self._mode == "starting" and (
                    (not self._socket_connected and now - self._started_at >= 10) or now - self._started_at >= 30
                )
            if timed_out:
                self.stop("reader", fault=True, result=result)
            return
        conflicts = self.apps.conflicting_apps()
        if conflicts:
            with self.apps.deps.state.lock_run():
                self.stop("app")
                self.apps.hold_locked()
            return
        outcome = self.attempts.outcome(pending.attempt_id) if pending is not None and pending.attempt_id else None
        should_stop = False
        with self._condition:
            if self._mode != "active":
                return
            if lan and self._pending is not None:
                self._pending = None
                self._show_locked("connected", hostname=self._hostname)
            elif lan and self._screen != "connected":
                should_stop = True
            elif pending is not None and self._pending == pending and outcome == "failed":
                self._pending = None
                self._show_locked("failed", reason="connect_failed")
            if self._screen in {"rejected", "failed"} and now - self._screen_at >= (
                5 if self._screen == "rejected" else 8
            ):
                self._show_locked("scanning")
            if self._screen == "connected" and now - self._screen_at >= 60:
                should_stop = True
        if should_stop:
            self.stop("connected")
            self._restore()

    def run_forever(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.tick()
            except Exception:
                logger.warning("wifi-setup: observation failed")
            self._shutdown.wait(2)

    def shutdown(self) -> None:
        self._shutdown.set()
        self.stop("shutdown")
