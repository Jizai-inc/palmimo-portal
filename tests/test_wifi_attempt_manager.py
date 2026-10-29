from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import pytest

from palmimo_portal.core.wifi_attempt_manager import (
    AttemptInProgressError,
    CleanupFailedError,
    KickUnavailableError,
    Kind,
    WifiAttemptManager,
    WifiConnectFailed,
)
from palmimo_portal.ports import (
    AdapterUnavailableError,
    ConnectionState,
    PolkitDeniedError,
    WifiAttempt,
    WifiDeviceStatus,
    WifiStatus,
)
from palmimo_portal.testing.fakes import FakeComitupServicePort, FakeStateStore, FakeWifiProfilePort


class Profiles(FakeWifiProfilePort):
    def __init__(self) -> None:
        super().__init__()
        self.fault: Callable[[str], None] = lambda operation: None
        self.read_fault = False
        self.disconnect_fault = False
        self.delayed_disconnect = False
        self.restore_fault = False

    def add_profile(self, *args: Any, **kwargs: Any) -> str:
        uid = super().add_profile(*args, **kwargs)
        self.fault("add")
        return uid

    def update_profile(self, *args: Any, **kwargs: Any) -> None:
        if self.restore_fault and "user_data" in kwargs and "palmimo.attempt.id" not in kwargs["user_data"]:
            raise OSError("restore failed")
        super().update_profile(*args, **kwargs)
        self.fault("update")

    def delete_profile(self, uuid: str) -> None:
        self.fault("delete")
        super().delete_profile(uuid)

    def device_status(self) -> WifiDeviceStatus:
        if self.read_fault:
            raise OSError("NM offline")
        return super().device_status()

    def disconnect_device(self) -> None:
        if not self.delayed_disconnect:
            super().disconnect_device()
        if self.disconnect_fault:
            raise OSError("disconnect failed")


class Service(FakeComitupServicePort):
    def __init__(self) -> None:
        super().__init__()
        self.starts = 0
        self.on_start: Callable[[], None] = lambda: None
        self.stop_fault = False
        self.start_faults = 0

    def stop(self) -> None:
        if self.stop_fault:
            raise OSError("stop failed")
        super().stop()

    def start(self) -> None:
        self.starts += 1
        if self.start_faults:
            self.start_faults -= 1
            raise OSError("start failed")
        super().start()
        self.on_start()


@dataclass
class Rig:
    nm: Profiles = field(default_factory=Profiles)
    service: Service = field(default_factory=Service)
    state: FakeStateStore = field(default_factory=FakeStateStore)
    now: float = 0
    pending: list[Callable[[], None]] = field(default_factory=list)
    tick: Callable[[], None] = lambda: None
    lock: Any = field(default_factory=threading.RLock)

    def manager(self, **kwargs: Any) -> WifiAttemptManager:
        return WifiAttemptManager(
            self.nm,
            self.service,
            self.state,
            edit_lock=self.lock,
            legacy_connect=kwargs.pop("legacy_connect", lambda *args: None),
            legacy_status=kwargs.pop("legacy_status", lambda: WifiStatus(ConnectionState.CONNECTING, None, None)),
            monotonic=lambda: self.now,
            sleep=self.sleep,
            spawn=kwargs.pop("spawn", self.pending.append),
            **kwargs,
        )

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.tick()

    def add(self, ssid: bytes = b"home", priority: int = 2, password: str = "correct") -> str:
        return self.nm.add_profile(
            ssid, password, name=ssid.decode(errors="replace"), priority=priority, user_data={"owner": "other"}
        )

    def snapshot(self) -> Any:
        return self.nm.list_profiles(), dict(self.nm.psks)

    def run(self) -> None:
        self.pending.pop(0)()


def test_wrong_password_switch_returns_to_original_network_without_changing_saved_settings() -> None:
    rig = Rig()
    home = rig.add()
    rig.nm.device = WifiDeviceStatus(100, home)
    before = rig.snapshot()
    manager = rig.manager()
    assert manager.outcome("unrecognized") == "unknown"
    uid = manager.start_wifi_attempt("credentials", b"away", "wrong", None)
    assert manager.outcome(uid) == "attempting"
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, home)) if rig.now >= 16 else None
    rig.run()
    assert manager.outcome(uid) == "failed"
    assert rig.snapshot() == before
    assert rig.nm.device_status() == WifiDeviceStatus(100, home)
    assert rig.state.read_last_wifi_attempt().observed_connection_name == "home"  # type: ignore[union-attr]


def test_wrong_password_from_ap_restores_same_raw_ssid_password_and_marks() -> None:
    rig = Rig()
    rig.add(b"\xff\xfeAB")
    rig.nm.device = WifiDeviceStatus(100, "setup-ap")
    before = rig.snapshot()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"\xff\xfeAB", "wrong", None)
    assert any(p.ssid.startswith(b"palmimo-parked-") for p in rig.nm.list_profiles())
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, "setup-ap")) if rig.now >= 35 else None
    rig.run()
    assert manager.outcome(uid) == "failed"
    assert rig.snapshot() == before
    assert rig.state.read_last_wifi_attempt().observed_connection_name is None  # type: ignore[union-attr]


@pytest.mark.parametrize("kind", ["credentials", "activate_saved"])
def test_success_adopts_target_removes_duplicates_and_restores_other_priorities(kind: Kind) -> None:
    rig = Rig()
    original = rig.add()
    duplicate = rig.add()
    other = rig.add(b"other", 999)
    manager = rig.manager()
    uid = manager.start_wifi_attempt(
        kind, b"home", "new-password" if kind == "credentials" else None, original if kind == "activate_saved" else None
    )
    target = next(
        p.uuid for p in rig.nm.list_profiles() if p.user_data.get("palmimo.attempt.role") in {"try", "selected"}
    )
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target))
    rig.run()
    profiles = {p.uuid: p for p in rig.nm.list_profiles()}
    assert manager.outcome(uid) == "connected"
    assert duplicate not in profiles
    assert profiles[other].priority == 999
    assert profiles[target].ssid == b"home"
    assert profiles[target].name == "home"
    assert profiles[target].priority == (999 if kind == "credentials" else 2)
    assert rig.nm.psks[target] == ("new-password" if kind == "credentials" else "correct")
    assert all(not any(k.startswith("palmimo.attempt.") for k in p.user_data) for p in profiles.values())


@pytest.mark.parametrize("failure", ["add", "update", "spawn", "state"])
def test_start_failure_rereads_marks_and_restores_settings_without_kicking(failure: str) -> None:
    rig = Rig()
    rig.add()
    rig.add(b"other", 999)
    before = rig.snapshot()

    def fault(operation: str) -> None:
        if operation == failure:
            rig.nm.fault = lambda operation: None
            raise OSError("reply lost after write")

    rig.nm.fault = fault
    manager = rig.manager()
    if failure == "spawn":

        def fail_spawn(work: Callable[[], None]) -> None:
            raise OSError("cannot start thread")

        manager = rig.manager(spawn=fail_spawn)
    if failure == "state":

        def fail_state(attempt: WifiAttempt) -> None:
            raise OSError("disk full")

        rig.state.write_last_wifi_attempt = fail_state  # type: ignore[method-assign]
    with pytest.raises(WifiConnectFailed):
        manager.start_wifi_attempt("credentials", b"home", "wrong", None)
    assert rig.snapshot() == before
    assert rig.service.starts == 0
    assert not manager.in_progress


def test_active_attempt_and_saved_edits_are_rejected_without_changes() -> None:
    rig = Rig()
    rig.add()
    manager = rig.manager()
    manager.start_wifi_attempt("credentials", b"away", "wrong", None)
    before = rig.snapshot()
    with pytest.raises(AttemptInProgressError):
        manager.start_wifi_attempt("credentials", b"elsewhere", "wrong", None)
    with pytest.raises(AttemptInProgressError), manager.saved_network_edit():
        pytest.fail("edit admitted")
    assert rig.snapshot() == before


@pytest.mark.parametrize("fault", ["stop", "disconnect", "start", "stuck"])
def test_kick_failure_restores_settings_attempts_start_and_clears_busy(fault: str) -> None:
    rig = Rig()
    home = rig.add()
    rig.nm.device = WifiDeviceStatus(100, home)
    before = rig.snapshot()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"home", "wrong", None)
    rig.service.stop_fault = fault == "stop"
    rig.service.start_faults = 1 if fault == "start" else 0
    rig.nm.disconnect_fault = fault == "disconnect"
    rig.nm.delayed_disconnect = fault in {"disconnect", "stuck"}
    rig.run()
    assert manager.outcome(uid) == "failed"
    assert rig.snapshot() == before
    assert rig.service.running
    assert not manager.in_progress


@pytest.mark.parametrize("device_state", [0, 10, 20, 30, 40, 100, 110, 120])
def test_disconnect_error_is_tolerated_only_after_device_is_disconnected(device_state: int) -> None:
    rig = Rig()
    rig.nm.device = WifiDeviceStatus(device_state, None)
    rig.nm.delayed_disconnect = True
    rig.nm.disconnect_fault = True
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"home", "new-password", None)
    target = rig.nm.list_profiles()[0].uuid
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target))
    rig.run()
    assert manager.outcome(uid) == ("connected" if device_state in {0, 10, 20, 30} else "failed")


@pytest.mark.parametrize("read_fault", [False, True])
def test_more_candidates_extend_failure_wait_and_preserve_profiles(read_fault: bool) -> None:
    waits = []
    for saved_count in (1, 3):
        rig = Rig()
        for index in range(saved_count):
            rig.add(f"home-{index}".encode())
        before = rig.snapshot()
        manager = rig.manager()
        uid = manager.start_wifi_attempt("credentials", b"away", "wrong", None)
        rig.service.on_start = partial(setattr, rig.nm, "read_fault", read_fault)
        rig.run()
        waits.append(rig.now)
        assert manager.outcome(uid) == "failed"
        assert rig.snapshot() == before
        assert not manager.in_progress
    assert waits[1] > waits[0]


@pytest.mark.parametrize("active", [False, True])
def test_restart_recovers_pending_attempt_using_current_nm_uuid(active: bool) -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"home", "new-password", None)
    target = next(p.uuid for p in rig.nm.list_profiles() if p.name.startswith("palmimo-try-"))
    rig.nm.device = WifiDeviceStatus(100, target if active else "setup-ap")
    recovered = rig.manager()
    recovered.cleanup_marked()
    assert recovered.outcome(uid) == ("connected" if active else "failed")
    assert rig.state.read_last_wifi_attempt().result == ("connected" if active else "failed")  # type: ignore[union-attr]
    assert all(not any(k.startswith("palmimo.attempt.") for k in p.user_data) for p in rig.nm.list_profiles())
    if not active:
        assert rig.snapshot() == before


def test_restart_finishes_adopted_cleanup_even_after_target_disconnects() -> None:
    rig = Rig()
    rig.add()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"home", "new-password", None)
    target = next(p.uuid for p in rig.nm.list_profiles() if p.name.startswith("palmimo-try-"))
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target))
    rig.nm.fault = lambda op: (_ for _ in ()).throw(OSError("NM offline")) if op == "delete" else None
    rig.run()
    assert manager.outcome(uid) == "connected"
    assert any(p.user_data.get("palmimo.attempt.phase") == "adopted" for p in rig.nm.list_profiles())
    rig.nm.fault = lambda operation: None
    rig.nm.device = WifiDeviceStatus(30, None)
    rig.manager().cleanup_marked()
    assert [(p.uuid, p.name) for p in rig.nm.list_profiles()] == [(target, "home")]
    assert rig.nm.psks[target] == "new-password"
    assert rig.nm.list_profiles()[0].user_data == {}


@pytest.mark.parametrize("entry", ["worker", "recovery"])
@pytest.mark.parametrize("kind", ["credentials", "activate_saved"])
def test_published_success_survives_crash_and_disconnected_recovery(
    monkeypatch: pytest.MonkeyPatch, entry: str, kind: Kind
) -> None:
    rig = Rig()
    original = rig.add()
    rig.add()
    manager = rig.manager()
    uid = manager.start_wifi_attempt(
        kind, b"home", "new-password" if kind == "credentials" else None, original if kind == "activate_saved" else None
    )
    target = next(
        p.uuid for p in rig.nm.list_profiles() if p.user_data.get("palmimo.attempt.role") in {"try", "selected"}
    )
    write_attempt = rig.state.write_last_wifi_attempt

    def crash_after_success(attempt: WifiAttempt) -> None:
        write_attempt(attempt)
        if attempt.result == "connected":
            raise SystemExit("power loss after success publication")

    with monkeypatch.context() as patch:
        patch.setattr(rig.state, "write_last_wifi_attempt", crash_after_success)
        with pytest.raises(SystemExit):
            if entry == "worker":
                rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target))
                rig.run()
            else:
                rig.nm.device = WifiDeviceStatus(100, target)
                rig.manager().cleanup_marked()

    rig.nm.device = WifiDeviceStatus(30, None)
    rig.manager().cleanup_marked()

    assert [(p.uuid, p.ssid) for p in rig.nm.list_profiles()] == [(target, b"home")]
    assert rig.nm.psks[target] == ("new-password" if kind == "credentials" else "correct")
    attempt = rig.state.read_last_wifi_attempt()
    assert attempt is not None and attempt.result == "connected"
    if entry == "worker":
        assert manager.outcome(uid) == "connected"


@pytest.mark.parametrize("failure", ["delete", "restore"])
def test_failed_cleanup_blocks_new_attempt_until_recovery_succeeds(failure: str) -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"home", "wrong", None)
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, "setup-ap"))
    rig.nm.fault = lambda op: (_ for _ in ()).throw(OSError("NM offline")) if op == failure else None
    rig.nm.restore_fault = failure == "restore"
    rig.run()
    assert manager.outcome(uid) == "failed"
    assert not manager.in_progress
    with pytest.raises(CleanupFailedError):
        manager.start_wifi_attempt("credentials", b"other", "wrong", None)
    rig.nm.fault = lambda operation: None
    rig.nm.restore_fault = False
    rig.manager().cleanup_marked()
    assert rig.snapshot() == before


def test_start_recovers_abandoned_marks_before_preparing_new_attempt() -> None:
    rig = Rig()
    rig.add()
    rig.manager().start_wifi_attempt("credentials", b"home", "wrong", None)
    recovered = rig.manager()
    recovered.start_wifi_attempt("credentials", b"away", "wrong", None)
    assert any(p.ssid == b"home" for p in rig.nm.list_profiles())
    assert not any(p.ssid.startswith(b"palmimo-parked-") for p in rig.nm.list_profiles())


def test_orphaned_attempting_record_is_failed_at_startup() -> None:
    rig = Rig()
    rig.state.write_last_wifi_attempt(WifiAttempt("home", "attempting", 1))
    rig.manager().cleanup_marked()
    assert rig.state.read_last_wifi_attempt().result == "failed"  # type: ignore[union-attr]


@pytest.mark.parametrize("denied", [True, False])
def test_platform_probe_distinguishes_legacy_from_unavailable_backend(denied: bool) -> None:
    rig = Rig()
    error = (
        PolkitDeniedError("comitup.service", "start")
        if denied
        else AdapterUnavailableError("system_backend_unavailable", "offline")
    )
    rig.service.raise_on_call = error
    legacy_calls: list[tuple[Any, ...]] = []

    def legacy(*args: Any) -> str:
        legacy_calls.append(args)
        return "legacy-id"

    manager = rig.manager(legacy_connect=legacy)
    manager.check_platform()
    if denied:
        assert manager.outcome(manager.start_wifi_attempt("credentials", b"home", "secret", None)) == "attempting"
        assert legacy_calls == [("credentials", b"home", "secret", None)]
    else:
        with pytest.raises(KickUnavailableError):
            manager.start_wifi_attempt("credentials", b"home", "secret", None)
    assert rig.nm.list_profiles() == []


def test_known_network_cache_uses_only_successful_reads_and_excludes_trials() -> None:
    rig = Rig()
    manager = rig.manager()
    manager.start_wifi_attempt("credentials", b"home", "wrong", None)
    assert not manager.has_known_networks()
    rig.add()
    assert not manager.has_known_networks()
    rig.now = 2
    assert manager.has_known_networks()
    rig.now = 4
    rig.nm.raise_on_call = OSError("offline")
    with pytest.raises(OSError):
        manager.has_known_networks()
    rig.nm.raise_on_call = None
    assert manager.has_known_networks()


def test_visible_profiles_and_connection_name_hide_internal_names() -> None:
    rig = Rig()
    home = rig.add(b"\xffhome")
    manager = rig.manager()
    manager.start_wifi_attempt("credentials", b"\xffhome", "wrong", None)
    assert manager.visible_profiles() == []
    assert manager.connection_name(home) == "\ufffdhome"
    assert manager.has_known_networks()


def test_saved_activation_success_preserves_custom_connection_name() -> None:
    rig = Rig()
    saved = rig.add()
    rig.nm.update_profile(saved, name="Custom profile")
    manager = rig.manager()
    manager.start_wifi_attempt("activate_saved", b"home", None, saved)
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, saved))
    rig.run()
    assert rig.nm.list_profiles()[0].name == "Custom profile"


def test_activation_after_deadline_is_failed_and_active_trial_is_deleted() -> None:
    rig = Rig()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"home", "password", None)
    target = rig.nm.list_profiles()[0].uuid

    def activated_late() -> None:
        if rig.now >= 90:
            rig.now += 1
            rig.nm.device = WifiDeviceStatus(100, target)

    rig.tick = activated_late
    rig.run()
    assert manager.outcome(uid) == "failed"
    assert rig.nm.list_profiles() == []


def test_restart_cleanup_excludes_new_attempts_and_saved_edits_while_kicking() -> None:
    rig = Rig()
    rig.add()
    rig.manager().start_wifi_attempt("credentials", b"home", "wrong", None)
    recovered = rig.manager()
    reached: list[bool] = []

    def on_start() -> None:
        with pytest.raises(AttemptInProgressError):
            recovered.start_wifi_attempt("credentials", b"away", "wrong", None)
        with pytest.raises(AttemptInProgressError), recovered.saved_network_edit():
            pytest.fail("edit admitted")
        reached.append(True)

    rig.service.on_start = on_start
    recovered.cleanup_marked()
    assert reached == [True]


def test_worker_failure_to_write_result_still_restores_settings() -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    manager = rig.manager()
    manager.start_wifi_attempt("credentials", b"home", "wrong", None)

    def fail_state(attempt: WifiAttempt) -> None:
        raise OSError("disk full")

    rig.state.write_last_wifi_attempt = fail_state  # type: ignore[method-assign]
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, "setup-ap"))
    rig.run()
    assert rig.snapshot() == before
    assert not manager.in_progress


@pytest.mark.parametrize("operation", ["update", "add"])
def test_power_loss_during_preparation_recovers_from_only_persisted_marks(operation: str) -> None:
    class PowerLoss(BaseException):
        pass

    rig = Rig()
    rig.add(b"\xff\xfeAB")
    rig.add(b"other", 999)
    before = rig.snapshot()

    def cut(op: str) -> None:
        if op == operation:
            raise PowerLoss

    rig.nm.fault = cut
    with pytest.raises(PowerLoss):
        rig.manager().start_wifi_attempt("credentials", b"\xff\xfeAB", "wrong", None)
    rig.nm.fault = lambda operation: None
    rig.manager().cleanup_marked()
    assert rig.snapshot() == before


@pytest.mark.parametrize("priority", [2, 999])
def test_saved_activation_failure_restores_all_original_profiles(priority: int) -> None:
    rig = Rig()
    selected = rig.add(priority=priority)
    rig.add()
    other = rig.add(b"other", 999)
    before = rig.snapshot()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("activate_saved", b"home", None, selected)
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, other))
    rig.run()
    assert manager.outcome(uid) == "failed"
    assert rig.snapshot() == before


@pytest.mark.parametrize("intermediate_state", [40, 50, 60, 70, 80, 90, 100, 110, 120])
def test_disconnect_completion_is_observed_before_comitup_can_reuse_old_connection(intermediate_state: int) -> None:
    rig = Rig()
    home = rig.add()
    rig.nm.device = WifiDeviceStatus(100, home)
    rig.nm.delayed_disconnect = True
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"away", "password", None)
    target = next(p.uuid for p in rig.nm.list_profiles() if p.name.startswith("palmimo-try-"))

    def tick() -> None:
        if rig.service.running:
            rig.nm.device = WifiDeviceStatus(100, target)
        elif rig.now >= 3:
            rig.nm.device = WifiDeviceStatus(30, None)
        else:
            rig.nm.device = WifiDeviceStatus(intermediate_state, home)

    rig.tick = tick
    seen: list[int] = []
    rig.service.on_start = lambda: seen.append(rig.nm.device_status().state)
    rig.run()
    assert manager.outcome(uid) == "connected"
    assert seen == [30]


def test_unavailable_profile_reads_cannot_leave_outcome_attempting_after_worker_exit() -> None:
    rig = Rig()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"home", "password", None)
    rig.nm.raise_on_call = OSError("NM offline")
    rig.run()
    assert manager.outcome(uid) == "failed"
    assert rig.state.read_last_wifi_attempt().result == "failed"  # type: ignore[union-attr]
    assert not manager.in_progress
    rig.nm.raise_on_call = None
    rig.manager().cleanup_marked()
    assert rig.nm.list_profiles() == []


def test_completed_outcome_remains_available_after_later_web_trials() -> None:
    rig = Rig()
    manager = rig.manager()
    first = manager.start_wifi_attempt("credentials", b"first", "password")
    rig.run()
    assert manager.outcome(first) == "failed"
    second = manager.start_wifi_attempt("credentials", b"second", "password")
    assert manager.outcome(first) == "failed"
    rig.run()
    manager.start_wifi_attempt("credentials", b"third", "password")
    assert manager.outcome(first) == "failed"
    assert manager.outcome(second) == "failed"
    rig.run()


@pytest.mark.parametrize("stage", ["prepare", "rollback", "worker"])
def test_credential_failures_do_not_log_passwords(
    stage: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig()
    manager = rig.manager()
    password = "secret-password"

    def fault(operation: str) -> None:
        if operation == "add" or stage == "rollback":
            raise OSError(password)

    if stage in {"prepare", "rollback"}:
        rig.nm.fault = fault
        with pytest.raises(WifiConnectFailed):
            manager.start_wifi_attempt("credentials", b"home", password)
    else:

        def stop() -> None:
            raise OSError(password)

        monkeypatch.setattr(rig.service, "stop", stop)
        manager.start_wifi_attempt("credentials", b"home", password)
        rig.run()
    assert password not in caplog.text
