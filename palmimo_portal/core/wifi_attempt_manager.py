"""Pending marks retain rollback values; adopted marks commit success until cleanup ends.

Roles: try is disposable, parked hides a duplicate SSID, lowered yields priority,
selected temporarily promotes a saved profile. Cleanup removes all completed marks.
comitup uses only one profile per SSID and tries autoconnect-disabled candidates,
so duplicates need a different SSID. Restart retains an active connection; stop,
disconnect, then start forces candidate selection. A missing candidate takes about
25 seconds, so each candidate receives a 30-second budget plus 60 seconds overhead.
NM priorities are bounded to -999..999; device states 30 and 100 are DISCONNECTED and ACTIVATED.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Literal

from palmimo_portal.ports import (
    AdapterUnavailableError,
    ComitupServicePort,
    ConnectionState,
    NetworkPort,
    PolkitDeniedError,
    SavedNetworkNotFoundError,
    SavedNetworkPort,
    StateStore,
    WifiAttempt,
    WifiProfile,
    WifiProfilePort,
    WifiStatus,
)


logger = logging.getLogger("palmimo_portal")
Kind = Literal["credentials", "activate_saved"]
Outcome = Literal["attempting", "connected", "failed", "unknown"]
LegacyConnect = Callable[[Kind, bytes, str | None, str | None], str | None]
_MARK = "palmimo.attempt."
_DISCONNECTED = 30
_ACTIVATED = 100
_DISCONNECT_TIMEOUT_SECONDS = 30


class AttemptInProgressError(Exception):
    pass


class CleanupFailedError(Exception):
    pass


class WifiConnectFailed(Exception):  # noqa: N818
    pass


class KickUnavailableError(Exception):
    pass


def connect_legacy(
    network: NetworkPort, saved: SavedNetworkPort, kind: Kind, ssid: bytes, psk: str | None, saved_uuid: str | None
) -> None:
    name = ssid.decode("utf-8")
    if kind == "activate_saved":
        saved.activate(name)
    elif network.get_status().state is ConnectionState.CONNECTED:
        saved.save(name, psk or "", first=True)
        saved.activate(name)
    else:
        network.connect(name, psk or "")


def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="palmimo-wifi-attempt").start()


class WifiAttemptManager:
    """Recover Wi-Fi edits from durable profile marks; edit_lock must also guard saved-network writes."""

    def __init__(
        self,
        profiles: WifiProfilePort,
        service: ComitupServicePort,
        state: StateStore,
        *,
        edit_lock: threading.RLock,
        legacy_connect: LegacyConnect,
        legacy_status: Callable[[], WifiStatus],
        spawn_recovery: Callable[[Callable[[], None]], None] = _spawn,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        spawn: Callable[[Callable[[], None]], None] = _spawn,
    ) -> None:
        self._profiles = profiles
        self._service = service
        self._state = state
        self._edit_lock = edit_lock
        self._operation_lock = threading.Lock()
        self._operation_owner: int | None = None
        self._recovering = False
        self._recovery_stop = threading.Event()
        self._spawn_recovery = spawn_recovery
        self._monotonic = monotonic
        self._sleep = sleep
        self._spawn = spawn
        self._legacy_connect = legacy_connect
        self._legacy_status = legacy_status
        self._legacy = False
        self._platform_error: Exception | None = None
        self._active: tuple[str, float] | None = None
        self._last_id: str | None = None
        self._result: Outcome = "unknown"
        self._completed_outcomes: dict[str, Outcome] = {}
        self._known_cache: tuple[float, bool] | None = None

    @property
    def in_progress(self) -> bool:
        with self._edit_lock:
            return self._active is not None or self._recovering

    def check_platform(self) -> None:
        logger.info("Wi-Fi platform probe: starting comitup.service")
        try:
            self._service.start()
        except PolkitDeniedError:
            if not self._legacy:
                logger.warning("comitup control denied; using legacy Wi-Fi connection flow")
            self._legacy = True
            self._platform_error = None
        except Exception as error:
            self._platform_error = error
            logger.error("Wi-Fi platform probe failed")
        else:
            self._legacy = False
            self._platform_error = None

    def _ensure_platform(self) -> None:
        if self._platform_error is not None:
            self.check_platform()
            if self._platform_error is not None:
                raise KickUnavailableError from self._platform_error

    def start_recovery(self, *, delay: float = 0) -> None:
        """The 90-second window starts at this call and includes the platform probe."""
        with self._edit_lock:
            if self._recovering or self._active is not None:
                raise AttemptInProgressError
            self._recovering = True
            self._recovery_stop.clear()
            deadline = self._monotonic() + 90

        def recover() -> None:
            try:
                with self._operation_lock:
                    if delay:
                        self._sleep(delay)
                    self.check_platform()
                    while not self._recovery_stop.is_set():
                        try:
                            with self._edit_lock:
                                marked = self._marked()
                                attempt = self._state.read_last_wifi_attempt()
                            if any(p.user_data.get(_MARK + "phase") == "adopted" for p in marked):
                                break
                            if not marked and (attempt is None or attempt.result != "attempting"):
                                break
                            status = self._profiles.device_status()
                            if status.state == _ACTIVATED and any(
                                p.uuid == status.active_uuid for p in self._profiles.list_profiles()
                            ):
                                break
                        except AdapterUnavailableError:
                            if self._monotonic() >= deadline:
                                raise
                        if self._monotonic() >= deadline:
                            break
                        self._sleep(1)
                    if not self._recovery_stop.is_set():
                        self._recover_marked()
            except Exception:
                logger.error("Wi-Fi background recovery failed")
            finally:
                with self._edit_lock:
                    self._recovering = False

        try:
            self._spawn_recovery(recover)
        except Exception:
            self._recovering = False
            raise

    def stop_recovery(self) -> None:
        """Shutdown does not wait for the daemon recovery thread to finish."""
        self._recovery_stop.set()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        if self._recovering or self._active is not None or self._operation_owner == threading.get_ident():
            raise AttemptInProgressError
        if not self._operation_lock.acquire(timeout=3):
            raise AttemptInProgressError
        self._operation_owner = threading.get_ident()
        try:
            with self._edit_lock:
                if self._active is not None or self._recovering:
                    raise AttemptInProgressError
            yield
        finally:
            self._operation_owner = None
            self._operation_lock.release()

    @contextmanager
    def saved_network_edit(self) -> Iterator[None]:
        with self._exclusive():
            self._recover_marked(kick=False)
            with self._edit_lock:
                try:
                    yield
                finally:
                    self._known_cache = None

    def start_wifi_attempt(
        self, kind: Kind, ssid_bytes: bytes, psk: str | None = None, saved_uuid: str | None = None
    ) -> str:
        with self._exclusive():
            return self._start(kind, ssid_bytes, psk, saved_uuid)

    def _start(self, kind: Kind, ssid_bytes: bytes, psk: str | None, saved_uuid: str | None) -> str:
        self._ensure_platform()
        self._recover_marked()
        if self._legacy or "\ufffd".encode() in ssid_bytes:
            if not self._legacy:
                logger.warning("lossy SSID requires the legacy Wi-Fi connection flow")
            return self._start_legacy(kind, ssid_bytes, psk, saved_uuid)
        attempt_id = uuid.uuid4().hex[:8]
        with self._edit_lock:
            if kind == "activate_saved" and saved_uuid is None:
                profile = next(
                    (
                        p
                        for p in sorted(self._profiles.list_profiles(), key=lambda p: -p.priority)
                        if p.ssid == ssid_bytes
                    ),
                    None,
                )
                if profile is None:
                    raise SavedNetworkNotFoundError(ssid_bytes.decode("utf-8", errors="replace"))
                saved_uuid = profile.uuid
            try:
                candidates = self._prepare(attempt_id, kind, ssid_bytes, psk, saved_uuid)
                self._state.write_last_wifi_attempt(
                    WifiAttempt(ssid_bytes.decode("utf-8", errors="replace"), "attempting", time.time())
                )
                self._active = (attempt_id, self._monotonic())
                self._last_id, self._result = attempt_id, "attempting"
            except Exception as error:
                logger.error("Wi-Fi preparation failed id=%s", attempt_id)
                self._rollback_start(attempt_id)
                raise WifiConnectFailed from error
        try:
            self._spawn(lambda: self._run(attempt_id, candidates))
        except Exception as error:
            logger.error("Wi-Fi worker launch failed id=%s", attempt_id)
            with self._edit_lock:
                try:
                    self._rollback_start(attempt_id)
                    self._record(attempt_id, "failed")
                finally:
                    self._active = None
            raise WifiConnectFailed from error
        return attempt_id

    def activate_saved(self, ssid: str) -> str:
        with self._exclusive():
            return self._start("activate_saved", ssid.encode(), None, None)

    def _start_legacy(self, kind: Kind, ssid: bytes, psk: str | None, saved_uuid: str | None) -> str:
        attempt_id = uuid.uuid4().hex[:8]
        self._last_id, self._result = attempt_id, "attempting"
        self._active = attempt_id, self._monotonic()
        try:
            self._state.write_last_wifi_attempt(WifiAttempt(ssid.decode(), "attempting", time.time()))
            self._legacy_connect(kind, ssid, psk, saved_uuid)
            self._known_cache = None
            self._spawn(lambda: self._run_legacy(attempt_id))
        except Exception as error:
            try:
                self._record(attempt_id, "failed")
            finally:
                self._active = None
            if isinstance(error, SavedNetworkNotFoundError):
                raise
            raise WifiConnectFailed from error
        return attempt_id

    def _run_legacy(self, attempt_id: str) -> None:
        result: Outcome = "failed"
        observed = None
        started = self._monotonic()
        try:
            while self._monotonic() - started < 90:
                self._sleep(1)
                try:
                    status = self._legacy_status()
                except Exception:
                    continue
                attempt = self._state.read_last_wifi_attempt()
                if status.state is ConnectionState.CONNECTED and attempt is not None and status.ssid == attempt.ssid:
                    result = "connected"
                    break
                if self._monotonic() - started >= 10 and status.state in {
                    ConnectionState.CONNECTED,
                    ConnectionState.UNPROVISIONED,
                }:
                    observed = status.ssid if status.state is ConnectionState.CONNECTED else None
                    break
        finally:
            with self._edit_lock:
                try:
                    self._record(attempt_id, result)
                    attempt = self._state.read_last_wifi_attempt()
                    if attempt is not None and observed is not None:
                        self._state.write_last_wifi_attempt(replace(attempt, observed_connection_name=observed))
                except Exception:
                    logger.error("legacy Wi-Fi outcome persistence failed")
                finally:
                    self._active = None

    def display_ssid(self, ssid: str | None) -> str | None:
        if ssid is None:
            return None
        with self._edit_lock:
            try:
                profiles = self._profiles.list_profiles()
            except AdapterUnavailableError:
                return ssid
            profile = next(
                (
                    p
                    for p in profiles
                    if p.ssid.decode("utf-8", errors="replace") == ssid and p.user_data.get(_MARK + "role") == "parked"
                ),
                None,
            )
            return (
                bytes.fromhex(profile.user_data[_MARK + "ssid"]).decode("utf-8", errors="replace") if profile else ssid
            )

    def _prepare(self, attempt_id: str, kind: Kind, ssid: bytes, psk: str | None, saved_uuid: str | None) -> int:
        profiles = self._profiles.list_profiles()
        selected = next((p for p in profiles if p.uuid == saved_uuid), None)
        if kind == "activate_saved" and (selected is None or selected.ssid != ssid):
            raise ValueError("invalid Wi-Fi attempt target")
        parked = lowered = 0
        for profile in profiles:
            if kind == "activate_saved" and profile.uuid == saved_uuid:
                role = "selected"
            elif profile.ssid == ssid:
                role = "parked"
                parked += 1
            elif profile.priority == 999:
                role = "lowered"
                lowered += 1
            else:
                continue
            data = dict(profile.user_data)
            data.update(
                {
                    _MARK + "id": attempt_id,
                    _MARK + "role": role,
                    _MARK + "phase": "pending",
                    _MARK + "priority": str(profile.priority),
                }
            )
            if role == "parked":
                data.update({_MARK + "ssid": profile.ssid.hex(), _MARK + "autoconnect": str(int(profile.autoconnect))})
                self._profiles.update_profile(
                    profile.uuid,
                    ssid=f"palmimo-parked-{attempt_id}".encode(),
                    autoconnect=False,
                    priority=-999,
                    user_data=data,
                )
            else:
                self._profiles.update_profile(profile.uuid, priority=999 if role == "selected" else 998, user_data=data)
        if kind == "credentials":
            self._profiles.add_profile(
                ssid,
                psk or "",
                name=f"palmimo-try-{attempt_id}",
                priority=999,
                user_data={_MARK + "id": attempt_id, _MARK + "role": "try", _MARK + "phase": "pending"},
            )
        self._known_cache = None
        logger.info(
            "Wi-Fi attempt started id=%s kind=%s ssid=%r parked=%d lowered=%d", attempt_id, kind, ssid, parked, lowered
        )
        return len(profiles) + int(kind == "credentials")

    def _marked(self, attempt_id: str | None = None) -> list[WifiProfile]:
        return [
            p
            for p in self._profiles.list_profiles()
            if _MARK + "id" in p.user_data and (attempt_id is None or p.user_data[_MARK + "id"] == attempt_id)
        ]

    def _rollback_start(self, attempt_id: str) -> None:
        try:
            self._cleanup(attempt_id, success=False, kick=False, persist=False)
        except Exception:
            logger.error("Wi-Fi preparation rollback failed for %s", attempt_id)

    def _kick(self, attempt_id: str) -> float:
        logger.info("Wi-Fi kick started id=%s", attempt_id)
        started = self._monotonic()
        error: Exception | None = None
        try:
            self._service.stop()
            try:
                self._profiles.disconnect_device()
            except Exception:
                if self._profiles.device_status().state > _DISCONNECTED:
                    raise
            while self._profiles.device_status().state > _DISCONNECTED:
                if self._monotonic() - started >= _DISCONNECT_TIMEOUT_SECONDS:
                    raise TimeoutError("Wi-Fi device did not disconnect")
                self._sleep(1)
        except Exception as caught:
            error = caught
        finally:
            for retry in range(2):
                try:
                    self._service.start()
                    break
                except Exception as start_error:
                    if retry == 1:
                        self._platform_error = start_error
                        if error is not None:
                            logger.error("Wi-Fi kick failed before restart id=%s", attempt_id)
                        logger.error("comitup restart failed id=%s; retaining recovery marks", attempt_id)
                        raise KickUnavailableError from start_error
                    logger.warning("retrying comitup start id=%s", attempt_id)
                    self._sleep(3)
        if error is not None:
            raise error
        logger.info("Wi-Fi kick completed id=%s", attempt_id)
        return started

    def _run(self, attempt_id: str, candidates: int) -> None:
        success = False
        restart_failed = False
        retry_cleanup = False
        observed_uuid = None
        try:
            started = self._kick(attempt_id)
            deadline = started + 60 + candidates * 30
            while self._monotonic() < deadline:
                self._sleep(1)
                if self._monotonic() >= deadline:
                    break
                try:
                    status = self._profiles.device_status()
                except Exception:
                    continue
                if status.state == _ACTIVATED:
                    observed_uuid = status.active_uuid
                    with self._edit_lock:
                        targets = {
                            p.uuid
                            for p in self._marked(attempt_id)
                            if p.user_data[_MARK + "role"] in {"try", "selected"}
                        }
                    success = status.active_uuid in targets
                    break
        except KickUnavailableError:
            restart_failed = True
            logger.error("Wi-Fi attempt %s could not restart comitup", attempt_id)
        except Exception:
            logger.error("Wi-Fi attempt %s failed", attempt_id)
        finally:
            logger.info(
                "Wi-Fi result id=%s result=%s active_uuid=%s",
                attempt_id,
                "connected" if success else "failed",
                observed_uuid,
            )
            try:
                if not success:
                    try:
                        with self._edit_lock:
                            self._record(attempt_id, "failed")
                    except Exception:
                        logger.error("Wi-Fi outcome persistence failed for %s", attempt_id)
                if not restart_failed:
                    self._cleanup(attempt_id, success=success, persist=success)
            except Exception:
                logger.error("Wi-Fi cleanup failed for %s", attempt_id)
                retry_cleanup = success and self.outcome(attempt_id) == "attempting"
            finally:
                with self._edit_lock:
                    self._active = None
                    if retry_cleanup:
                        try:
                            self.start_recovery(delay=3)
                        except Exception:
                            logger.exception("Wi-Fi cleanup recovery launch failed for %s", attempt_id)

    def _record(self, attempt_id: str, result: Outcome, *, only_pending: bool = False) -> None:
        self._last_id, self._result = attempt_id, result
        self._completed_outcomes[attempt_id] = result
        attempt = self._state.read_last_wifi_attempt()
        if attempt is None or (only_pending and attempt.result != "attempting"):
            return
        observed = None
        if result == "failed":
            try:
                status = self._profiles.device_status()
                if status.state == _ACTIVATED:
                    observed = self.connection_name(status.active_uuid)
                    if observed == attempt.ssid:
                        observed = None
            except Exception:
                pass
        self._state.write_last_wifi_attempt(replace(attempt, result=result, observed_connection_name=observed))

    def _restore(self, profile: WifiProfile, *, keep_mark: bool = False) -> None:
        data = profile.user_data
        restored_data = data if keep_mark else {k: v for k, v in data.items() if not k.startswith(_MARK)}
        if data[_MARK + "role"] == "parked":
            self._profiles.update_profile(
                profile.uuid,
                ssid=bytes.fromhex(data[_MARK + "ssid"]),
                autoconnect=bool(int(data[_MARK + "autoconnect"])),
                priority=int(data[_MARK + "priority"]),
                user_data=restored_data,
            )
        else:
            self._profiles.update_profile(profile.uuid, priority=int(data[_MARK + "priority"]), user_data=restored_data)

    def _cleanup(
        self, attempt_id: str, *, success: bool | None = None, kick: bool = True, persist: bool = True
    ) -> None:
        needs_kick = False
        with self._edit_lock:
            marked = self._marked(attempt_id)
            if not marked:
                return
            targets = [p for p in marked if p.user_data[_MARK + "role"] in {"try", "selected"}]
            target = targets[0] if targets else None
            if any(p.user_data.get(_MARK + "phase") == "adopted" for p in marked):
                success = True
            if success is None:
                try:
                    status = self._profiles.device_status()
                    success = target is not None and status.state == _ACTIVATED and status.active_uuid == target.uuid
                except Exception:
                    success = False
            if success:
                if target is None:
                    raise CleanupFailedError("adopted target missing")
                data = {**target.user_data, _MARK + "phase": "adopted"}
                self._profiles.update_profile(target.uuid, user_data=data)
            if persist:
                try:
                    self._record(attempt_id, "connected" if success else "failed", only_pending=True)
                except Exception:
                    logger.error("Wi-Fi outcome persistence failed for %s", attempt_id)
            if success:
                assert target is not None
                for profile in marked:
                    if profile.user_data[_MARK + "role"] == "parked":
                        self._profiles.delete_profile(profile.uuid)
                for profile in marked:
                    if profile.user_data[_MARK + "role"] == "lowered":
                        self._restore(profile)
                if target.user_data[_MARK + "role"] == "selected":
                    self._restore(replace(target, user_data=data), keep_mark=True)
                self._profiles.update_profile(
                    target.uuid,
                    priority=min(
                        max((p.priority for p in self._profiles.list_profiles() if p.uuid != target.uuid), default=0)
                        + 1,
                        999,
                    )
                    if target.user_data[_MARK + "role"] == "try"
                    else None,
                    name=target.ssid.decode("utf-8", errors="replace")
                    if target.user_data[_MARK + "role"] == "try"
                    else None,
                    user_data={k: v for k, v in target.user_data.items() if not k.startswith(_MARK)},
                )
            else:
                for profile in marked:
                    if profile.user_data[_MARK + "role"] == "try":
                        self._profiles.delete_profile(profile.uuid)
                restored = [p for p in marked if p.user_data[_MARK + "role"] != "try"]
                for profile in restored:
                    self._restore(profile, keep_mark=True)
                if restored and kick and not self._legacy:
                    try:
                        status = self._profiles.device_status()
                        infrastructure_active = status.state == _ACTIVATED and any(
                            p.uuid == status.active_uuid for p in self._profiles.list_profiles()
                        )
                        needs_kick = not infrastructure_active
                    except Exception:
                        logger.error("Wi-Fi cleanup could not determine active network id=%s", attempt_id)
            self._known_cache = None
        if needs_kick:
            self._kick(attempt_id)
        if not success:
            with self._edit_lock:
                for profile in restored:
                    self._restore(profile)
        logger.info(
            "Wi-Fi cleanup completed id=%s action=%s kick=%s",
            attempt_id,
            "adopted" if success else "restored",
            needs_kick,
        )

    def cleanup_marked(self) -> None:
        with self._exclusive():
            self._recover_marked()

    def _recover_marked(self, *, kick: bool = True) -> None:
        with self._edit_lock:
            try:
                groups = list(dict.fromkeys(p.user_data[_MARK + "id"] for p in self._marked()))
            except AdapterUnavailableError:
                raise
            except Exception as error:
                raise CleanupFailedError from error
        try:
            if groups:
                self._ensure_platform()
            for attempt_id in groups:
                self._cleanup(attempt_id, kick=kick)
            with self._edit_lock:
                attempt = self._state.read_last_wifi_attempt()
            if not groups and attempt is not None and attempt.result == "attempting":
                status = self._legacy_status()
                connected = status.state is ConnectionState.CONNECTED
                result = "connected" if connected and status.ssid == attempt.ssid else "failed"
                observed = status.ssid if connected and status.ssid != attempt.ssid else None
                with self._edit_lock:
                    self._state.write_last_wifi_attempt(
                        replace(attempt, result=result, observed_connection_name=observed)
                    )
        except AdapterUnavailableError:
            raise
        except Exception as error:
            raise CleanupFailedError from error

    def outcome(self, attempt_id: str) -> Outcome:
        """Completed trial results remain queryable for this process's lifetime."""
        with self._edit_lock:
            return self._result if attempt_id == self._last_id else self._completed_outcomes.get(attempt_id, "unknown")

    def has_known_networks(self) -> bool:
        with self._edit_lock:
            now = self._monotonic()
            if self._known_cache is not None and 0 <= now - self._known_cache[0] < 2:
                return self._known_cache[1]
            known = any(p.user_data.get(_MARK + "role") != "try" for p in self._profiles.list_profiles())
            self._known_cache = (now, known)
            return known

    def visible_profiles(self) -> list[WifiProfile]:
        with self._edit_lock:
            return [
                p for p in self._profiles.list_profiles() if p.user_data.get(_MARK + "role") not in {"try", "parked"}
            ]

    def connection_name(self, active_uuid: str | None) -> str | None:
        with self._edit_lock:
            profile = next((p for p in self._profiles.list_profiles() if p.uuid == active_uuid), None)
            if profile is None or profile.user_data.get(_MARK + "role") == "try":
                return None
            ssid = (
                bytes.fromhex(profile.user_data[_MARK + "ssid"])
                if profile.user_data.get(_MARK + "role") == "parked"
                else profile.ssid
            )
            return ssid.decode("utf-8", errors="replace")
