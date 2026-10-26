from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from palmimo_portal.core.wifi_attempt_manager import AttemptInProgressError
from palmimo_portal.ports import (
    AdapterUnavailableError,
    ConnectionState,
    PolkitDeniedError,
    SavedNetworkNotFoundError,
    WifiDeviceStatus,
    WifiStatus,
)
from tests.test_wifi_attempt_manager import Rig


def test_transient_platform_failure_is_reprobed_before_next_attempt() -> None:
    rig = Rig()
    manager = rig.manager()
    rig.service.start_faults = 1
    manager.check_platform()
    manager.start_wifi_attempt("credentials", b"home", "password", None)
    assert manager.in_progress
    assert rig.nm.list_profiles()[0].ssid == b"home"


@pytest.mark.parametrize(
    "restored,active,extra_start", [(True, "known", False), (False, "ap", False), (True, "ap", True)]
)
def test_failed_cleanup_restarts_only_restored_candidates_without_an_active_network(
    restored: bool, active: str, extra_start: bool
) -> None:
    rig = Rig()
    known = rig.add(priority=999 if restored and active == "known" else 2)
    rig.nm.device = WifiDeviceStatus(100, "ap" if active == "ap" else known)
    before = rig.snapshot()
    manager = rig.manager()
    manager.start_wifi_attempt("credentials", b"home" if restored and active == "ap" else b"away", "wrongpass", None)
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, known if active == "known" else "ap"))
    started = rig.service.starts
    rig.run()
    assert rig.snapshot() == before
    assert rig.service.starts - started == (2 if extra_start else 1)


def test_saved_edit_recovers_abandoned_marks_before_modifying_profiles() -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    rig.manager().start_wifi_attempt("credentials", b"home", "wrongpass", None)
    recovered = rig.manager()
    with recovered.saved_network_edit():
        assert rig.snapshot() == before
        rig.add(b"new")
    assert any(p.ssid == b"new" for p in rig.nm.list_profiles())


def test_unavailable_nm_is_not_relabelled_as_cleanup_failure() -> None:
    rig = Rig()
    rig.nm.raise_on_call = AdapterUnavailableError("network_backend_unavailable", "offline")
    with pytest.raises(AdapterUnavailableError):
        rig.manager().cleanup_marked()


def test_failed_service_restart_retains_marks_until_a_saved_edit_recovers() -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    manager = rig.manager()
    uid = manager.start_wifi_attempt("credentials", b"home", "wrongpass", None)
    rig.service.start_faults = 2
    rig.run()
    assert manager.outcome(uid) == "failed"
    assert not manager.in_progress
    assert any("palmimo.attempt.id" in p.user_data for p in rig.nm.list_profiles())
    assert rig.now > 0
    with manager.saved_network_edit():
        assert rig.snapshot() == before
    assert rig.service.running


def test_cleanup_restart_failure_keeps_restoration_marks_for_retry() -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    manager = rig.manager()
    manager.start_wifi_attempt("credentials", b"home", "wrongpass", None)
    rig.service.on_start = lambda: setattr(rig.service, "start_faults", 2)
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, "ap"))
    rig.run()
    assert any("palmimo.attempt.id" in p.user_data for p in rig.nm.list_profiles())
    rig.service.on_start = lambda: None
    with manager.saved_network_edit():
        assert rig.snapshot() == before


def test_adoption_priority_tracks_other_saved_profiles_instead_of_staying_maximal() -> None:
    rig = Rig()
    rig.add(priority=7)
    manager = rig.manager()
    manager.start_wifi_attempt("credentials", b"new", "password", None)
    target = next(p.uuid for p in rig.nm.list_profiles() if p.ssid == b"new")
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target))
    rig.run()
    assert next(p for p in rig.nm.list_profiles() if p.uuid == target).priority == 8


def test_legacy_recovery_restores_profiles_without_restarting_comitup() -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    rig.manager().start_wifi_attempt("credentials", b"home", "wrongpass", None)
    manager = rig.manager()
    rig.service.raise_on_call = PolkitDeniedError("comitup.service", "start")
    manager.check_platform()
    starts = rig.service.starts
    manager.cleanup_marked()
    assert rig.snapshot() == before
    assert rig.service.starts == starts


def test_startup_waits_for_pending_target_and_blocks_writes_without_holding_read_lock() -> None:
    rig = Rig()
    rig.add()
    rig.manager().start_wifi_attempt("credentials", b"home", "password", None)
    target = next(p.uuid for p in rig.nm.list_profiles() if p.name.startswith("palmimo-try-"))
    rig.pending.clear()
    sleeping = threading.Event()
    release = threading.Event()
    observed = threading.Event()
    known: list[bool] = []
    recovery_threads: list[threading.Thread] = []

    def spawn(work: Callable[[], None]) -> None:
        thread = threading.Thread(target=work)
        recovery_threads.append(thread)
        thread.start()

    def sleep(seconds: float) -> None:
        sleeping.set()
        release.wait(5)
        rig.nm.device = WifiDeviceStatus(100, target)
        rig.sleep(seconds)

    manager = rig.manager(spawn_recovery=spawn)
    manager._sleep = sleep
    manager.start_recovery()

    def read() -> None:
        known.append(manager.has_known_networks())
        observed.set()

    reader = threading.Thread(target=read)
    try:
        assert sleeping.wait(1)
        with pytest.raises(AttemptInProgressError):
            manager.start_wifi_attempt("credentials", b"other", "password", None)
        with pytest.raises(AttemptInProgressError), manager.saved_network_edit():
            pytest.fail("edit admitted")
        reader.start()
        assert observed.wait(1)
        assert known == [True]
    finally:
        release.set()
        for thread in recovery_threads:
            thread.join(2)
        if reader.ident is not None:
            reader.join(2)

    assert rig.state.read_last_wifi_attempt().result == "connected"  # type: ignore[union-attr]
    assert not manager.in_progress
    assert [(p.uuid, p.name) for p in rig.nm.list_profiles()] == [(target, "home")]


def test_startup_wait_expires_and_restores_unconnected_profiles() -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    rig.manager().start_wifi_attempt("credentials", b"home", "wrongpass", None)
    rig.pending.clear()
    manager = rig.manager(spawn_recovery=rig.pending.append)
    manager.start_recovery()
    rig.run()
    assert rig.now >= 90
    assert rig.snapshot() == before
    assert not manager.in_progress


def test_short_contention_waits_until_saved_edit_finishes() -> None:
    rig = Rig()
    manager = rig.manager()
    entered = threading.Event()
    release = threading.Event()

    def edit() -> None:
        with manager.saved_network_edit():
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=edit)
    thread.start()
    assert entered.wait(1)
    timer = threading.Timer(0.02, release.set)
    timer.start()
    try:
        manager.start_wifi_attempt("credentials", b"home", "password", None)
        assert manager.in_progress
    finally:
        release.set()
        thread.join(2)
        timer.join(2)


def test_legacy_activation_does_not_hold_profile_lock_during_network_calls() -> None:
    rig = Rig()
    rig.add()
    observed = threading.Event()
    readers: list[threading.Thread] = []
    manager = rig.manager()

    def legacy(*args: object) -> None:
        def read() -> None:
            manager.has_known_networks()
            observed.set()

        reader = threading.Thread(target=read)
        readers.append(reader)
        reader.start()
        assert observed.wait(1)

    manager = rig.manager(legacy_connect=legacy)
    rig.service.raise_on_call = PolkitDeniedError("comitup.service", "start")
    manager.check_platform()
    try:
        manager.activate_saved("home")
    finally:
        for reader in readers:
            reader.join(2)


@pytest.mark.parametrize("target_connects", [False, True])
def test_startup_ignores_activated_ap_until_target_connects_or_wait_expires(target_connects: bool) -> None:
    rig = Rig()
    rig.add()
    before = rig.snapshot()
    rig.manager().start_wifi_attempt("credentials", b"home", "password", None)
    target = next(p.uuid for p in rig.nm.list_profiles() if p.name.startswith("palmimo-try-"))
    rig.pending.clear()
    rig.nm.device = WifiDeviceStatus(100, "ap")
    manager = rig.manager(spawn_recovery=rig.pending.append)
    if target_connects:
        rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target)) if rig.now >= 5 else None
    manager.start_recovery()
    rig.run()
    attempt = rig.state.read_last_wifi_attempt()
    assert attempt is not None
    assert attempt.result == ("connected" if target_connects else "failed")
    assert rig.now >= (5 if target_connects else 90)
    if not target_connects:
        assert rig.snapshot() == before


@pytest.mark.parametrize("active_trial", [False, True])
def test_cleanup_clears_restored_marks_when_device_status_is_unavailable(active_trial: bool) -> None:
    rig = Rig()
    known = rig.add()
    before = rig.snapshot()
    rig.manager().start_wifi_attempt("credentials", b"home", "wrongpass", None)
    if active_trial:
        target = next(p.uuid for p in rig.nm.list_profiles() if p.name.startswith("palmimo-try-"))
        rig.nm.device = WifiDeviceStatus(100, target)
    rig.nm.read_fault = True
    starts = rig.service.starts
    recovered = rig.manager()
    recovered.cleanup_marked()
    assert rig.snapshot() == before
    assert [p.uuid for p in recovered.visible_profiles()] == [known]
    assert rig.service.starts == starts


@pytest.mark.parametrize("priority,target_ssid", [(2, b"home"), (999, b"away")])
def test_saved_edit_restores_profiles_without_disrupting_ap(priority: int, target_ssid: bytes) -> None:
    rig = Rig()
    rig.add(priority=priority)
    before = rig.snapshot()
    rig.manager().start_wifi_attempt("credentials", target_ssid, "wrongpass", None)
    rig.nm.device = WifiDeviceStatus(100, "ap")
    starts = rig.service.starts
    recovered = rig.manager()
    with recovered.saved_network_edit():
        assert rig.snapshot() == before
        rig.add(b"new")
    assert rig.nm.device == WifiDeviceStatus(100, "ap")
    assert rig.service.starts == starts
    assert any(p.ssid == b"new" for p in recovered.visible_profiles())


@pytest.mark.parametrize("ssid", ["palmimo-try-home", "palmimo-parked-x"])
def test_adopted_networks_with_internal_prefixes_remain_known_and_visible(ssid: str) -> None:
    rig = Rig()
    manager = rig.manager()
    manager.start_wifi_attempt("credentials", ssid.encode(), "password", None)
    target = next(p.uuid for p in rig.nm.list_profiles() if p.user_data.get("palmimo.attempt.role") == "try")
    rig.nm.update_profile(target, name="User chosen name")
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target))
    rig.run()
    assert manager.has_known_networks()
    assert [(p.uuid, p.ssid) for p in manager.visible_profiles()] == [(target, ssid.encode())]
    assert manager.connection_name(target) == ssid
    assert manager.display_ssid(ssid) == ssid


@pytest.mark.parametrize("reason", ["polkit", "lossy_ssid"])
@pytest.mark.parametrize("connected", ["target", "other", None])
@pytest.mark.parametrize("entry", ["startup", "cleanup", "next_attempt"])
def test_unmarked_legacy_attempt_recovery_uses_current_connection(
    reason: str, connected: str | None, entry: str
) -> None:
    rig = Rig()
    ssid = "home" if reason == "polkit" else "ho\ufffdme"
    target = rig.add(ssid.encode())
    other = rig.add(b"other")

    def status() -> WifiStatus:
        active = rig.nm.device.active_uuid if rig.nm.device.state == 100 else None
        name = ssid if active == target else "other" if active == other else None
        return WifiStatus(ConnectionState.CONNECTED if name else ConnectionState.UNPROVISIONED, name, None)

    original = rig.manager(legacy_status=status)
    if reason == "polkit":
        rig.service.raise_on_call = PolkitDeniedError("comitup.service", "start")
    original.check_platform()
    original.start_wifi_attempt("credentials", ssid.encode(), "password", None)
    rig.pending.clear()
    rig.nm.device = WifiDeviceStatus(100, "ap")
    recovered = rig.manager(legacy_status=status, spawn_recovery=rig.pending.append)

    def activate() -> None:
        if connected is not None:
            rig.nm.device = WifiDeviceStatus(100, target if connected == "target" else other)

    if entry == "startup":
        rig.tick = lambda: activate() if rig.now >= 5 else None
        recovered.start_recovery()
        with pytest.raises(AttemptInProgressError):
            recovered.start_wifi_attempt("credentials", b"new", "password", None)
        rig.run()
        assert rig.now >= (90 if connected is None else 5)
        assert not recovered.in_progress
    else:
        activate()
        if entry == "next_attempt":
            with pytest.raises(SavedNetworkNotFoundError):
                recovered.activate_saved("missing")
        else:
            recovered.cleanup_marked()
    attempt = rig.state.read_last_wifi_attempt()
    assert attempt is not None
    assert attempt.ssid == ssid
    assert attempt.result == ("connected" if connected == "target" else "failed")
    assert attempt.observed_connection_name == ("other" if connected == "other" else None)
