from __future__ import annotations

import threading

import pytest

from palmimo_portal.core.wifi_attempt_manager import AttemptInProgressError
from palmimo_portal.ports import AdapterUnavailableError, PolkitDeniedError, WifiDeviceStatus
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
    manager = rig.manager(spawn_recovery=rig.pending.append)
    manager.start_recovery()
    assert manager.in_progress
    with pytest.raises(AttemptInProgressError):
        manager.start_wifi_attempt("credentials", b"other", "password", None)
    with pytest.raises(AttemptInProgressError), manager.saved_network_edit():
        pytest.fail("edit admitted")
    assert manager.has_known_networks()
    rig.tick = lambda: setattr(rig.nm, "device", WifiDeviceStatus(100, target)) if rig.now >= 5 else None
    rig.run()
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
