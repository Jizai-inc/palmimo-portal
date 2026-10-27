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
    DisplayRecoveryPendingError,
    StartDeps,
    _start_app_locked,
    _stop_app_locked,
)
from palmimo_portal.core.wifi_attempt_manager import AttemptInProgressError, WifiAttemptManager
from palmimo_portal.core.wifi_qr import Rejection, make_url_qr, parse_wifi_qr
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


class UnknownAppDevicesError(Exception):
    def __init__(self, app: str) -> None:
        self.app = app
        super().__init__("Running app has no device declaration")


class WifiSetupApps:
    def __init__(self, deps: StartDeps, *, hostname: str = "palmimo") -> None:
        self.deps = deps
        self.hostname = hostname
        self._prepare_reason: str | None = None

    def conflicting_apps(self) -> dict[str, str]:
        apps = self.deps.state.read_apps_state().apps
        conflicts = {}
        for name in self.deps.app_unit.list_active_app_units():
            active_state = self.deps.app_unit.status(name).active_state
            if active_state not in RUNNING_ACTIVE_STATES:
                continue
            if name not in apps:
                raise UnknownAppDevicesError(name)
            manifest = read_manifest_for_app(self.deps.apps_job_ctx, apps[name])
            if SETUP_DEVICES.intersection(manifest.devices):
                conflicts[name] = active_state
        return conflicts

    def _refuse(self, reason: str, *, error: Exception | None = None) -> bool:
        if self._prepare_reason != reason:
            logger.log(
                logging.WARNING if error is not None else logging.INFO,
                "wifi-setup: prepare deferred reason=%s error_type=%s app=%s",
                reason,
                type(error).__name__ if error is not None else None,
                error.app if isinstance(error, UnknownAppDevicesError) else None,
            )
        self._prepare_reason = reason
        return False

    def prepare_locked(self) -> bool:
        """Caller holds run.lock through reader StartUnit."""
        try:
            record = self.deps.state.read_preempted_app()
            if record is not None and record.by != "wifi-setup":
                return self._refuse("other_recovery")
            conflicts = self.conflicting_apps()
            if "deactivating" in conflicts.values():
                return self._refuse("app_stopping")
            if record is not None and record.hold and conflicts:
                return self._refuse("user_hold")
            if len(conflicts) > 1:
                return self._refuse("multiple_apps")
            if conflicts:
                name = next(iter(conflicts))
                try:
                    self.deps.state.write_preempted_app(PreemptedApp(name, "wifi-setup"))
                except Exception as error:
                    return self._refuse("record_write_failed", error=error)
                try:
                    _stop_app_locked(self.deps, name)
                except Exception:
                    logger.warning("wifi-setup: app stop failed app=%s", name)
                deadline = time.monotonic() + 20
                while self.deps.app_unit.status(name).active_state in RUNNING_ACTIVE_STATES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return self._refuse("app_stop_timeout")
                    time.sleep(min(0.1, remaining))
                logger.info("wifi-setup: preempted app=%s", name)
        except Exception as error:
            return self._refuse("app_observation_failed", error=error)
        self._prepare_reason = None
        return True

    def hold_locked(self) -> None:
        record = self.deps.state.read_preempted_app()
        if record is not None and record.by == "display-firmware":
            raise DisplayRecoveryPendingError
        if record is not None and record.by != "wifi-setup":
            raise AppBusyError
        self.deps.state.write_preempted_app(PreemptedApp(None, "wifi-setup", hold=True))
        logger.info("wifi-setup: hold reason=app_start")

    def clear_hold(self) -> None:
        record = self.deps.state.read_preempted_app()
        if record is None or record.by != "wifi-setup" or not record.hold:
            return
        with self.deps.state.lock_run():
            record = self.deps.state.read_preempted_app()
            if record is not None and record.by == "wifi-setup" and record.hold:
                self.deps.state.write_preempted_app(
                    replace(record, hold=False) if record.preempted_app is not None else None
                )

    def restore(self, *, hold: bool) -> bool:
        record = self.deps.state.read_preempted_app()
        if record is None or record.by != "wifi-setup" or record.preempted_app is None:
            return True
        try:
            with self.deps.state.lock_apps(), self.deps.state.lock_run():
                record = self.deps.state.read_preempted_app()
                if record is None or record.by != "wifi-setup" or record.preempted_app is None:
                    return True
                name = record.preempted_app
                result = "already_running"
                try:
                    active_state = self.deps.app_unit.status(name).active_state
                    if active_state == "deactivating":
                        return False
                    if active_state not in RUNNING_ACTIVE_STATES:
                        _start_app_locked(
                            replace(self.deps, before_start=lambda devices: None, platform_ready=lambda: True),
                            name,
                            host=self.hostname,
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
    reason: str | None = None


@dataclass(frozen=True)
class Pending:
    attempt_id: str | None
    accepted_at: float


class WifiSetup:
    """Never hold the setup condition across D-Bus calls, trial operations, or socket close.

    Reader protocol: see doc/wifi-qr-reader.md.
    """

    APP_STOP_TIMEOUT = 10.0
    READER_STOP_TIMEOUT = 6.0
    CONNECTED_SCREEN_SECONDS = 300.0

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
        hostname: str,
    ) -> None:
        self.network = network
        self.profiles = profiles
        self.attempts = attempts
        self.unit = unit
        self.apps = apps
        self._factory = client_factory
        self._now = monotonic
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
        self._conflict_warning = False
        self._unavailable_reason: str | None = None
        self._client: WifiQrClientPort | None = None
        self._socket_connected = False
        self._disconnected = False
        self._generation = 0
        self._restore_hold: bool | None = None
        self._shutdown = threading.Event()

    def status(self) -> WifiSetupStatus:
        with self._condition:
            return WifiSetupStatus(
                self._mode, self._screen, self._unavailable_reason if self._mode == "unavailable" else None
            )

    def _show_locked(self, state: str, **fields: Any) -> None:
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
        if message.get("type") == "close":
            with self._condition:
                if generation is not None and generation != self._generation:
                    return
                accepted = self._mode == "active" and self._screen == "connected"
            if accepted:
                self.stop("connected")
                self._restore()
            return
        with self._condition:
            if generation is not None and generation != self._generation:
                return
            if message.get("type") == "hello" and self._mode == "starting":
                self._mode = "active"
                logger.info("wifi-setup: reader active")
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
            if generation == self._generation and self._mode in {"starting", "active"} and self._client is not None:
                self._disconnected = True
                self._socket_connected = False
                self._client = None

    def _connect_socket(self) -> None:
        with self._condition:
            if self._mode != "starting" or self._socket_connected:
                return
            generation = self._generation

        def message(value: dict[str, Any]) -> None:
            self.on_message(value, generation=generation)

        client = self._factory(message, lambda: self._socket_lost(generation))
        with self._condition:
            stale = self._mode != "starting" or generation != self._generation
            if not stale:
                self._client = client
        if stale:
            client.close()
            return
        try:
            client.connect()
        except Exception:
            with self._condition:
                if self._client is client:
                    self._client = None
            client.close()
            return
        with self._condition:
            stale = (
                generation != self._generation or self._mode not in {"starting", "active"} or self._client is not client
            )
            if not stale:
                self._socket_connected = True
        if stale:
            client.close()

    def _unavailable_locked(self, reason: str, *, retry_s: float | None = None) -> None:
        if self._mode != "unavailable" or self._unavailable_reason != reason:
            logger.warning("wifi-setup: unavailable reason=%s retry_s=%s", reason, retry_s)
        self._unavailable_reason = reason
        self._mode = "unavailable"
        self._retry_at = self._now() + retry_s if retry_s is not None else None

    def _enter(self, *, known: bool) -> None:
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
                    self._disconnected = False
                    self._conflict_warning = False
                if not self.apps.prepare_locked():
                    with self._condition:
                        if self._generation == generation:
                            self._mode = "idle"
                            self._condition.notify_all()
                    return
                with self._condition:
                    if self._generation != generation:
                        return
                    self._started_at = self._now()
                logger.info("wifi-setup: entering reason=%s", "known_network_lost" if known else "unprovisioned")
                self.unit.start()
                with self._condition:
                    stale = self._generation != generation or self._shutdown.is_set()
                if stale:
                    self.unit.stop_and_wait(timeout_s=self.READER_STOP_TIMEOUT)
                    return
        except RunLockTimeoutError:
            return
        except WifiQrUnitMissingError:
            with self._condition:
                self._unavailable_locked("unit_missing")
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

    def startup(self) -> None:
        logger.info("wifi-setup: stopping reason=startup")
        try:
            self.unit.stop_and_wait(timeout_s=self.READER_STOP_TIMEOUT)
        except WifiQrUnitMissingError:
            with self._condition:
                self._unavailable_locked("unit_missing")
        except PolkitDeniedError:
            logger.error("wifi-setup: startup reader stop denied")
        except Exception:
            logger.warning("wifi-setup: startup reader stop failed")

    def recover_startup(self, *, enabled: bool = True) -> bool:
        record = self.apps.deps.state.read_preempted_app()
        if record is None or record.by != "wifi-setup":
            return False
        if record.preempted_app is None:
            with self.apps.deps.state.lock_run():
                current = self.apps.deps.state.read_preempted_app()
                if current is not None and current.by == "wifi-setup" and current.preempted_app is None:
                    self.apps.deps.state.write_preempted_app(None)
            return False
        if not enabled:
            self.apps.restore(hold=False)
            return True
        try:
            status = self.network.get_status()
            if (
                status.state is ConnectionState.CONNECTED
                and status.ip_address
                and self.profiles.device_status().state == 100
            ):
                self.apps.clear_hold()
                self.apps.restore(hold=False)
        except Exception:
            logger.warning("wifi-setup: startup app recovery deferred")
        return True

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
        if result is None:
            try:
                result = self.unit.status()
            except Exception:
                logger.warning("wifi-setup: reader exit status unavailable")
        try:
            self.unit.stop_and_wait(timeout_s=self.READER_STOP_TIMEOUT)
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
                if code in {69, 76, 78}:
                    self._unavailable_locked(
                        {69: "dependencies_missing", 76: "firmware_too_old", 78: "devices_missing"}[code],
                        retry_s=3600 if code == 78 else None,
                    )
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
            logger.info(
                "wifi-setup: stopped reason=%s result=%s exec_main_code=%s exec_main_status=%s state=%s retry_s=%s",
                reason,
                result.result if result is not None else None,
                result.exec_main_code if result is not None else None,
                result.exec_main_status if result is not None else None,
                self._mode,
                max(0, self._retry_at - self._now()) if self._retry_at is not None else None,
            )
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
        if self._shutdown.is_set():
            return
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
                self._enter(known=known)
            return
        if mode not in {"starting", "active"}:
            if lan:
                self.apps.restore(hold=False)
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
                logger.warning(
                    "wifi-setup: reader deadline reason=%s",
                    "hello_timeout" if self._socket_connected else "socket_timeout",
                )
                self.stop("reader", fault=True, result=result)
            return
        outcome = self.attempts.outcome(pending.attempt_id) if pending is not None and pending.attempt_id else None
        should_stop = False
        with self._condition:
            if self._mode != "active":
                return
            if lan and self._pending is not None:
                self._pending = None
                url = f"http://{self._hostname}.local"
                self._show_locked("connected", url=url, qr=make_url_qr(url))
            elif lan and self._screen != "connected":
                should_stop = True
            elif (
                pending is not None
                and self._pending == pending
                and (outcome == "failed" or now - pending.accepted_at >= 120)
            ):
                self._pending = None
                self._show_locked("failed", reason="connect_failed")
            if self._screen in {"rejected", "failed"} and now - self._screen_at >= (
                5 if self._screen == "rejected" else 8
            ):
                self._show_locked("scanning")
            if self._screen == "connected" and now - self._screen_at >= self.CONNECTED_SCREEN_SECONDS:
                should_stop = True
        if should_stop:
            self.stop("connected")
            self._restore()
            return
        try:
            conflicts = self.apps.conflicting_apps()
        except Exception as error:
            if not self._conflict_warning:
                logger.warning(
                    "wifi-setup: app observation failed error_type=%s app=%s",
                    type(error).__name__,
                    error.app if isinstance(error, UnknownAppDevicesError) else None,
                )
            self._conflict_warning = True
            conflicts = {}
        else:
            self._conflict_warning = False
        if conflicts:
            try:
                with self.apps.deps.state.lock_run():
                    with self._condition:
                        if self._mode != "active":
                            return
                    self.stop("app")
                    self.apps.hold_locked()
            except RunLockTimeoutError:
                return

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
