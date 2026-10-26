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
_ACTIVATED = 100


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
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        spawn: Callable[[Callable[[], None]], None] = _spawn,
        legacy_connect: LegacyConnect | None = None,
        legacy_status: Callable[[], WifiStatus] | None = None,
    ) -> None:
        self._profiles = profiles
        self._service = service
        self._state = state
        self._edit_lock = edit_lock
        self._operation_lock = threading.Lock()
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
        self._known_cache: tuple[float, bool] | None = None

    @property
    def in_progress(self) -> bool:
        with self._edit_lock:
            return self._active is not None

    def check_platform(self) -> None:
        with self._edit_lock:
            try:
                self._service.start()
            except PolkitDeniedError:
                if not self._legacy:
                    logger.warning("comitup control denied; using legacy Wi-Fi connection flow")
                self._legacy = True
                self._platform_error = None
            except Exception as error:
                self._platform_error = error
            else:
                self._legacy = False
                self._platform_error = None

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        if not self._operation_lock.acquire(blocking=False):
            raise AttemptInProgressError
        try:
            with self._edit_lock:
                if self._active is not None:
                    raise AttemptInProgressError
            yield
        finally:
            self._operation_lock.release()

    @contextmanager
    def saved_network_edit(self) -> Iterator[None]:
        with self._exclusive(), self._edit_lock:
            if self._marked():
                raise AttemptInProgressError
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
        self._recover_marked()
        with self._edit_lock:
            if self._legacy or "\ufffd".encode() in ssid_bytes:
                if not self._legacy:
                    logger.warning("lossy SSID requires the legacy Wi-Fi connection flow")
                return self._start_legacy(kind, ssid_bytes, psk, saved_uuid)
            if self._platform_error is not None:
                raise KickUnavailableError from self._platform_error
            attempt_id = uuid.uuid4().hex[:8]
            try:
                candidates = self._prepare(attempt_id, kind, ssid_bytes, psk, saved_uuid)
                self._state.write_last_wifi_attempt(
                    WifiAttempt(ssid_bytes.decode("utf-8", errors="replace"), "attempting", time.time())
                )
                self._active = (attempt_id, self._monotonic())
                self._last_id, self._result = attempt_id, "attempting"
            except Exception as error:
                self._rollback_start(attempt_id)
                raise WifiConnectFailed from error
        try:
            self._spawn(lambda: self._run(attempt_id, candidates))
        except Exception as error:
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
            self._recover_marked()
            with self._edit_lock:
                if self._legacy or "\ufffd" in ssid:
                    return self._start("activate_saved", ssid.encode(), None, None)
                profile = next(
                    (
                        p
                        for p in sorted(self._profiles.list_profiles(), key=lambda p: -p.priority)
                        if p.ssid.decode("utf-8", errors="replace") == ssid
                    ),
                    None,
                )
                if profile is None:
                    raise SavedNetworkNotFoundError(ssid)
                return self._start("activate_saved", profile.ssid, None, profile.uuid)

    def _start_legacy(self, kind: Kind, ssid: bytes, psk: str | None, saved_uuid: str | None) -> str:
        if self._legacy_connect is None:
            raise KickUnavailableError("legacy connection flow is not configured")
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
                    if self._legacy_status is None:
                        break
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
                    logger.exception("legacy Wi-Fi outcome persistence failed")
                finally:
                    self._active = None

    def display_ssid(self, ssid: str | None) -> str | None:
        if ssid is None or not ssid.startswith(("palmimo-try-", "palmimo-parked-")):
            return ssid
        if ssid.startswith("palmimo-try-"):
            return None
        with self._edit_lock:
            profile = next(
                (
                    p
                    for p in self._profiles.list_profiles()
                    if p.ssid.decode("utf-8", errors="replace") == ssid and p.user_data.get(_MARK + "role") == "parked"
                ),
                None,
            )
            return (
                bytes.fromhex(profile.user_data[_MARK + "ssid"]).decode("utf-8", errors="replace") if profile else None
            )

    def _prepare(self, attempt_id: str, kind: Kind, ssid: bytes, psk: str | None, saved_uuid: str | None) -> int:
        profiles = self._profiles.list_profiles()
        selected = next((p for p in profiles if p.uuid == saved_uuid), None)
        if kind not in {"credentials", "activate_saved"} or (
            kind == "activate_saved" and (selected is None or selected.ssid != ssid)
        ):
            raise ValueError("invalid Wi-Fi attempt target")
        for profile in profiles:
            if kind == "activate_saved" and profile.uuid == saved_uuid:
                role = "selected"
            elif profile.ssid == ssid:
                role = "parked"
            elif profile.priority == 999:
                role = "lowered"
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
            logger.exception("Wi-Fi preparation rollback failed for %s", attempt_id)

    def _kick(self) -> float:
        started = self._monotonic()
        error: Exception | None = None
        try:
            self._service.stop()
            try:
                self._profiles.disconnect_device()
            except Exception:
                if self._profiles.device_status().state == _ACTIVATED:
                    raise
            # comitup adopts an existing active connection at startup, bypassing candidate order.
            while self._profiles.device_status().state == _ACTIVATED:
                if self._monotonic() - started >= 30:
                    raise TimeoutError("wlan0 did not disconnect")
                self._sleep(1)
        except Exception as caught:
            error = caught
        finally:
            try:
                self._service.start()
            except Exception:
                logger.exception("comitup start failed")
                raise
        if error is not None:
            raise error
        return started

    def _run(self, attempt_id: str, candidates: int) -> None:
        success = False
        try:
            started = self._kick()
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
                    with self._edit_lock:
                        targets = {
                            p.uuid
                            for p in self._marked(attempt_id)
                            if p.user_data[_MARK + "role"] in {"try", "selected"}
                        }
                    success = status.active_uuid in targets
                    break
        except Exception:
            logger.exception("Wi-Fi attempt %s failed", attempt_id)
        finally:
            try:
                try:
                    with self._edit_lock:
                        self._record(attempt_id, "connected" if success else "failed")
                except Exception:
                    logger.exception("Wi-Fi outcome persistence failed for %s", attempt_id)
                self._cleanup(attempt_id, success=success, persist=False)
            except Exception:
                logger.exception("Wi-Fi cleanup failed for %s", attempt_id)
            finally:
                with self._edit_lock:
                    self._active = None

    def _record(self, attempt_id: str, result: Outcome, *, only_pending: bool = False) -> None:
        self._last_id, self._result = attempt_id, result
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
            if persist:
                try:
                    self._record(attempt_id, "connected" if success else "failed", only_pending=True)
                except Exception:
                    logger.exception("Wi-Fi outcome persistence failed for %s", attempt_id)
            if success:
                if target is None:
                    raise CleanupFailedError("adopted target missing")
                data = {**target.user_data, _MARK + "phase": "adopted"}
                # Keep the commit mark until every destructive cleanup operation has completed.
                self._profiles.update_profile(target.uuid, user_data=data)
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
                    name=target.ssid.decode("utf-8", errors="replace")
                    if target.user_data[_MARK + "role"] == "try"
                    else None,
                    user_data={k: v for k, v in target.user_data.items() if not k.startswith(_MARK)},
                )
            else:
                for profile in marked:
                    if profile.user_data[_MARK + "role"] == "try":
                        self._profiles.delete_profile(profile.uuid)
                for profile in marked:
                    if profile.user_data[_MARK + "role"] != "try":
                        self._restore(profile)
                needs_kick = True
                if all(p.user_data[_MARK + "role"] == "try" for p in marked):
                    try:
                        status = self._profiles.device_status()
                        needs_kick = status.state != _ACTIVATED or self.connection_name(status.active_uuid) is None
                    except Exception:
                        pass
            self._known_cache = None
        if kick and needs_kick:
            self._kick()

    def cleanup_marked(self) -> None:
        with self._exclusive():
            self._recover_marked()

    def _recover_marked(self) -> None:
        with self._edit_lock:
            try:
                groups = list(dict.fromkeys(p.user_data[_MARK + "id"] for p in self._marked()))
            except Exception as error:
                raise CleanupFailedError from error
        try:
            for attempt_id in groups:
                self._cleanup(attempt_id)
            with self._edit_lock:
                attempt = self._state.read_last_wifi_attempt()
                if not groups and attempt is not None and attempt.result == "attempting":
                    self._state.write_last_wifi_attempt(replace(attempt, result="failed"))
        except Exception as error:
            raise CleanupFailedError from error

    def outcome(self, attempt_id: str) -> Outcome:
        with self._edit_lock:
            return self._result if attempt_id == self._last_id else "unknown"

    def has_known_networks(self) -> bool:
        with self._edit_lock:
            now = self._monotonic()
            if self._known_cache is not None and 0 <= now - self._known_cache[0] < 2:
                return self._known_cache[1]
            known = any(not p.name.startswith("palmimo-try-") for p in self._profiles.list_profiles())
            self._known_cache = (now, known)
            return known

    def visible_profiles(self) -> list[WifiProfile]:
        with self._edit_lock:
            return [
                p
                for p in self._profiles.list_profiles()
                if not p.name.startswith(("palmimo-try-", "palmimo-parked-"))
                and p.user_data.get(_MARK + "role") != "parked"
            ]

    def connection_name(self, active_uuid: str | None) -> str | None:
        with self._edit_lock:
            profile = next((p for p in self._profiles.list_profiles() if p.uuid == active_uuid), None)
            if profile is None or profile.name.startswith("palmimo-try-"):
                return None
            ssid = (
                bytes.fromhex(profile.user_data[_MARK + "ssid"])
                if profile.user_data.get(_MARK + "role") == "parked"
                else profile.ssid
            )
            return ssid.decode("utf-8", errors="replace")
