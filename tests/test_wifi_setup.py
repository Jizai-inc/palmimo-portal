import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.adapters.wifi_qr_socket import WifiQrSocketClient
from palmimo_portal.core.apps_start import AppBusyError, AppJobInProgressStartError, StartDeps, start_app
from palmimo_portal.core.wifi_attempt_manager import AttemptInProgressError
from palmimo_portal.core.wifi_setup import WifiSetup, WifiSetupApps
from palmimo_portal.ports import (
    AdapterUnavailableError,
    ConnectionState,
    PolkitDeniedError,
    PreemptedApp,
    UnitStatus,
    WifiDeviceStatus,
    WifiQrUnitMissingError,
    WifiQrUnitStatus,
    WifiStatus,
)
from palmimo_portal.testing.fakes import FakeAdapterBundle, FakeClockPort, FakeWifiQrUnitPort
from palmimo_portal.testing.wifi_qr_reader import FakeWifiQrReader
from tests.test_wifi_setup_apps import prepared as prepared


@dataclass
class Reader:
    messages: list[dict[str, Any]] = field(default_factory=list)
    connect_error: bool = False
    closed: bool = False

    def connect(self) -> None:
        if self.connect_error:
            raise ConnectionError

    def send(self, message: dict[str, Any]) -> bool:
        self.messages.append(message)
        return True

    def close(self) -> None:
        self.closed = True


@dataclass
class Rig:
    setup: WifiSetup
    adapters: FakeAdapterBundle
    unit: FakeWifiQrUnitPort
    clock: FakeClockPort
    reader: Reader

    def advance(self, seconds: float) -> None:
        end = self.clock.monotonic_value + seconds
        while self.clock.monotonic_value < end:
            self.clock.monotonic_value = min(end, self.clock.monotonic_value + 2)
            self.setup.tick()

    def active(self) -> None:
        self.setup.tick()
        self.advance(6)
        self.setup.on_message({"type": "hello", "proto": 1, "display": "frame"})
        assert self.setup.status().mode == "active"


@pytest.fixture
def rig(prepared: tuple[StartDeps, FakeAdapterBundle], monkeypatch: pytest.MonkeyPatch) -> Rig:
    deps, adapters = prepared
    clock = FakeClockPort()
    unit = FakeWifiQrUnitPort(
        monotonic=clock.monotonic,
        sleep=lambda seconds: setattr(clock, "monotonic_value", clock.monotonic() + seconds),
    )
    reader = Reader()
    adapters.wifi_profiles.device = WifiDeviceStatus(100, None)
    setup = WifiSetup(
        network=adapters.network,
        profiles=adapters.wifi_profiles,
        attempts=adapters.wifi_attempts,
        unit=unit,
        apps=WifiSetupApps(deps, hostname="robot"),
        client_factory=lambda on_message, on_disconnect: reader,
        monotonic=clock.monotonic,
        hostname="robot",
    )
    monkeypatch.setattr(adapters.wifi_attempts, "start_wifi_attempt", lambda *a: "attempt-one")
    return Rig(setup, adapters, unit, clock, reader)


def test_setup_waits_for_observed_ap_interval(rig: Rig) -> None:
    rig.setup.tick()
    rig.advance(4)
    assert rig.setup.status().mode == "idle"
    rig.advance(2)
    assert rig.setup.status().mode == "starting"


def test_setup_does_not_count_failed_network_polls(rig: Rig) -> None:
    rig.setup.tick()
    rig.advance(2)
    rig.adapters.network.raise_on_get_status = AdapterUnavailableError("offline", "no bus")
    rig.advance(60)
    assert rig.setup.status().mode == "idle"
    rig.adapters.network.raise_on_get_status = None
    rig.advance(2)
    assert rig.setup.status().mode == "idle"
    rig.advance(2)
    assert rig.setup.status().mode == "starting"


@pytest.mark.parametrize("connected,ip", [(ConnectionState.CONNECTING, None), (ConnectionState.CONNECTED, None)])
def test_setup_keeps_scanning_without_lan_address(rig: Rig, connected: ConnectionState, ip: str | None) -> None:
    rig.active()
    rig.adapters.network.status = WifiStatus(connected, "known", ip)
    rig.setup.tick()
    assert rig.setup.status().mode == "active"
    assert rig.setup.status().screen == "scanning"


def test_setup_stops_without_success_screen_for_non_qr_connection(rig: Rig) -> None:
    rig.active()
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "known", "192.0.2.1")
    rig.setup.tick()
    assert rig.setup.status().mode == "idle"
    assert rig.reader.closed
    assert not any(message["state"] == "connected" for message in rig.reader.messages)


@pytest.mark.parametrize("qr,reason", [("poster", "not_wifi"), ("WIFI:T:WEP;S:home;;", "unsupported_security")])
def test_setup_rejects_qr_then_resumes_scanning(rig: Rig, qr: str, reason: str) -> None:
    rig.active()
    rig.setup.on_message({"type": "qr", "text": qr})
    assert rig.reader.messages[-1] == {"type": "screen", "state": "rejected", "reason": reason}
    rig.advance(4)
    assert rig.setup.status().screen == "rejected"
    rig.advance(2)
    assert rig.setup.status().screen == "scanning"


def test_setup_marks_connecting_before_attempt_and_discards_extra_qr(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def attempt(*args: Any) -> str:
        assert rig.setup.status().screen == "connecting"
        assert rig.reader.messages[-1]["state"] == "connecting"
        calls.append(args)
        return "attempt-one"

    monkeypatch.setattr(rig.adapters.wifi_attempts, "start_wifi_attempt", attempt)
    rig.active()
    for _ in range(2):
        rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:secret-password;;"})
    assert calls == [("credentials", b"home", "secret-password")]
    assert "secret-password" not in caplog.text
    assert "WIFI:" not in caplog.text


@pytest.mark.parametrize(
    "failure,state,reason",
    [
        (AttemptInProgressError(), "rejected", "busy"),
        (RuntimeError("secret-password"), "failed", "connect_failed"),
    ],
)
def test_setup_attempt_failure_is_sanitized(
    rig: Rig,
    failure: Exception,
    state: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def attempt(*args: Any) -> str:
        raise failure

    monkeypatch.setattr(rig.adapters.wifi_attempts, "start_wifi_attempt", attempt)
    rig.active()
    rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:secret-password;;"})
    assert rig.reader.messages[-1] == {"type": "screen", "state": state, "reason": reason}
    assert "secret-password" not in caplog.text
    rig.advance(8)
    assert rig.setup.status().screen == "scanning"


def test_setup_uses_attempt_outcome_for_failure(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.active()
    rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    monkeypatch.setattr(rig.adapters.wifi_attempts, "outcome", lambda attempt_id: "failed")
    rig.setup.tick()
    assert rig.reader.messages[-1] == {"type": "screen", "state": "failed", "reason": "connect_failed"}
    rig.advance(6)
    assert rig.setup.status().screen == "failed"
    rig.advance(2)
    assert rig.setup.status().screen == "scanning"


def test_setup_accepts_connection_to_a_different_lan(rig: Rig) -> None:
    rig.active()
    rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "another-network", "192.0.2.1")
    rig.setup.tick()
    assert rig.setup.status().screen == "connected"
    assert rig.reader.messages[-1]["url"] == "http://192.0.2.1"
    assert "ssid" not in rig.reader.messages[-1]


@pytest.mark.parametrize(
    "code,mode,delay",
    [(69, "unavailable", None), (78, "unavailable", 3600), (75, "backoff", 30), (1, "backoff", 30), (0, "idle", None)],
)
def test_setup_classifies_reader_exit(rig: Rig, code: int, mode: str, delay: int | None) -> None:
    rig.active()
    rig.unit.unit_status = WifiQrUnitStatus(
        "failed" if code else "inactive", "loaded", "exit-code" if code else "success", 1, code
    )
    rig.setup.tick()
    assert rig.setup.status().mode == mode
    if delay is not None:
        rig.clock.monotonic_value += delay - 1
        rig.setup.tick()
        assert rig.setup.status().mode == mode
        rig.advance(2)
        assert rig.setup.status().mode in {"idle", "starting"}


@pytest.mark.parametrize("connect_error,deadline", [(True, 10), (False, 30)])
def test_setup_times_out_socket_or_hello(rig: Rig, connect_error: bool, deadline: int) -> None:
    rig.reader.connect_error = connect_error
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == "starting"
    rig.advance(deadline)
    assert rig.setup.status().mode == "backoff"
    assert rig.reader.closed


def test_setup_shutdown_preserves_preempted_record(rig: Rig) -> None:
    rig.active()
    record = PreemptedApp("face", "wifi-setup")
    rig.adapters.state.write_preempted_app(record)
    rig.setup.shutdown()
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.state.read_preempted_app() == record


def test_setup_waits_for_networkmanager_activation(rig: Rig) -> None:
    rig.active()
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    rig.adapters.wifi_profiles.device = WifiDeviceStatus(70, None)
    rig.setup.tick()
    assert rig.setup.status().mode == "active"
    assert rig.setup.status().screen == "scanning"


def test_setup_ignores_callback_from_previous_reader(rig: Rig) -> None:
    callbacks: list[Any] = []

    def client(message: Any, disconnect: Any) -> Reader:
        callbacks.append(message)
        return rig.reader

    rig.setup._factory = client
    rig.active()
    rig.setup.stop("app")
    rig.reader.closed = False
    rig.setup.tick()
    assert rig.setup.status().mode == "starting"
    callbacks[0]({"type": "hello", "proto": 1, "display": "frame"})
    assert rig.setup.status().mode == "starting"


def test_setup_web_start_timeout_keeps_record(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.active()
    record = PreemptedApp("face", "wifi-setup")
    rig.adapters.state.write_preempted_app(record)
    stopped = threading.Event()
    release = threading.Event()

    def stop() -> None:
        stopped.set()
        assert release.wait(2)
        rig.unit.unit_status = WifiQrUnitStatus("inactive")

    monkeypatch.setattr(rig.unit, "stop", stop)
    monkeypatch.setattr(rig.setup, "APP_STOP_TIMEOUT", 0.05)
    with rig.adapters.state.lock_run(), pytest.raises(AppBusyError):
        rig.setup.before_app_start(["camera"])
    assert stopped.is_set()
    assert rig.adapters.state.read_preempted_app() == record
    release.set()
    with rig.setup._condition:
        assert rig.setup._condition.wait_for(lambda: rig.setup.status().mode == "idle", timeout=2)


@pytest.mark.parametrize(
    "error,expected",
    [
        (WifiQrUnitMissingError(), "unavailable"),
        (PolkitDeniedError("palmimo-wifi-qr.service", "start"), "stopping"),
    ],
)
def test_setup_unit_failure_does_not_preempt_app(rig: Rig, error: Exception, expected: str) -> None:
    rig.unit.error = error
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == expected
    assert rig.adapters.app_unit.stop_calls == []


def test_setup_retries_exponentially_until_hello(rig: Rig) -> None:
    rig.reader.connect_error = True
    rig.setup.tick()
    rig.advance(16)
    assert rig.setup.status().mode == "backoff"
    rig.advance(30)
    assert rig.setup.status().mode == "starting"
    rig.advance(10)
    assert rig.setup.status().mode == "backoff"
    rig.advance(30)
    assert rig.setup.status().mode == "backoff"
    rig.advance(30)
    assert rig.setup.status().mode == "starting"
    rig.reader.connect_error = False
    rig.setup.tick()
    rig.setup.on_message({"type": "hello", "proto": 1, "display": "expr"})
    rig.unit.unit_status = WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 1)
    rig.setup.tick()
    assert rig.setup.status().mode == "backoff"
    rig.advance(30)
    assert rig.setup.status().mode == "starting"


@pytest.mark.parametrize(
    "error,expected",
    [
        (WifiQrUnitMissingError(), "unavailable"),
        (PolkitDeniedError("palmimo-wifi-qr.service", "stop"), "idle"),
        (RuntimeError("unavailable"), "idle"),
    ],
)
def test_setup_startup_stop_failure_allows_startup_to_continue(rig: Rig, error: Exception, expected: str) -> None:
    rig.unit.error = error
    rig.setup.startup()
    assert rig.setup.status().mode == expected


def test_setup_preempts_and_restores_app_after_lan_return(rig: Rig) -> None:
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.active()
    assert rig.adapters.app_unit.stop_calls == ["face"]
    assert rig.adapters.state.read_preempted_app() == PreemptedApp("face", "wifi-setup")
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "known", "192.0.2.1")
    rig.setup.tick()
    assert rig.adapters.app_unit.start_calls == ["face"]
    assert rig.adapters.state.read_preempted_app() is None


def test_setup_web_start_sets_hold_before_app_runs(rig: Rig) -> None:
    rig.active()
    deps = replace(rig.setup.apps.deps, before_start=rig.setup.before_app_start)
    start_app(deps, "face", host="robot")
    assert rig.setup.status().mode == "idle"
    assert rig.reader.closed
    assert rig.adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)
    rig.advance(60)
    assert rig.setup.status().mode == "idle"


def test_setup_nonconflicting_web_app_keeps_reader_active(rig: Rig) -> None:
    rig.active()
    rig.setup.before_app_start(["audio"])
    assert rig.setup.status().mode == "active"
    assert rig.adapters.state.read_preempted_app() is None


def test_setup_outside_app_start_stops_reader_and_sets_hold(rig: Rig) -> None:
    rig.active()
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.setup.tick()
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)


def test_setup_cleans_up_start_that_finishes_after_shutdown(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    original_start = rig.unit.start

    def start() -> None:
        rig.setup.shutdown()
        original_start()

    monkeypatch.setattr(rig.unit, "start", start)
    rig.setup.tick()
    rig.advance(6)
    assert rig.unit.status().active_state == "inactive"
    assert rig.setup.status().mode == "idle"


@pytest.mark.parametrize("code,result", [(69, "signal"), (78, "timeout"), (75, "signal")])
def test_setup_does_not_interpret_signal_as_exit_code(rig: Rig, code: int, result: str) -> None:
    rig.active()
    rig.unit.unit_status = WifiQrUnitStatus("failed", "loaded", result, 2, code)
    rig.setup.tick()
    assert rig.setup.status().mode == "backoff"


def test_setup_caps_retry_interval_at_ten_minutes(rig: Rig) -> None:
    rig.reader.connect_error = True
    rig.setup.tick()
    rig.advance(6)
    for interval in [30, 60, 120, 240, 480, 600, 600]:
        rig.advance(10)
        assert rig.setup.status().mode == "backoff"
        rig.advance(interval - 2)
        assert rig.setup.status().mode == "backoff"
        rig.advance(2)
        assert rig.setup.status().mode == "starting"


@pytest.mark.parametrize("initial", [ConnectionState.UNPROVISIONED, ConnectionState.CONNECTING])
def test_setup_completes_qr_flow_over_unix_socket(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    initial: ConnectionState,
) -> None:
    rig.adapters.network.status = WifiStatus(initial, None, None)
    monkeypatch.chdir(tmp_path)
    accepted = threading.Event()

    def attempt(*args: Any) -> str:
        accepted.set()
        return "socket-attempt"

    monkeypatch.setattr(rig.adapters.wifi_attempts, "start_wifi_attempt", attempt)
    with caplog.at_level(logging.INFO), FakeWifiQrReader(Path("reader.sock")) as reader:
        rig.setup._factory = lambda message, disconnect: WifiQrSocketClient(
            message,
            on_disconnect=disconnect,
            path=reader.path,
        )
        rig.setup.tick()
        rig.advance(6)
        reader.send({"type": "hello", "proto": 1, "display": "frame"})
        if initial is ConnectionState.CONNECTING:
            with rig.setup._condition:
                assert rig.setup._condition.wait_for(lambda: rig.setup.status().screen == "waiting", timeout=2)
            rig.adapters.network.status = WifiStatus(ConnectionState.UNPROVISIONED, None, None)
            rig.setup.tick()
            rig.advance(6)
        assert reader.receive() == {"type": "screen", "state": "scanning"}
        reader.send({"type": "qr", "text": "WIFI:S:home;P:secret-password;;"})
        assert reader.receive()["state"] == "checking"
        assert reader.receive() == {"type": "screen", "state": "connecting", "ssid": "home"}
        assert accepted.wait(2)
        rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "another", "192.0.2.1")
        rig.setup.tick()
        connected = reader.receive()
        assert connected["state"] == "connected"
        assert connected["url"] == "http://192.0.2.1"
        rig.advance(300)
        assert rig.setup.status().mode == "idle"
    assert "secret-password" not in caplog.text
    assert "WIFI:" not in caplog.text


def test_setup_reader_failure_restores_preempted_app_and_holds(rig: Rig) -> None:
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.active()
    rig.unit.unit_status = WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 75)
    rig.setup.tick()
    rig.setup.tick()
    assert rig.adapters.app_unit.start_calls == ["face"]
    assert rig.adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.advance(30)
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.app_unit.stop_calls == ["face"]


def test_setup_record_failure_keeps_mode_idle(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))

    def fail(record: PreemptedApp | None) -> None:
        raise OSError("full")

    monkeypatch.setattr(rig.adapters.state, "write_preempted_app", fail)
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.app_unit.stop_calls == []
    assert "start" not in rig.unit.calls


def test_setup_does_not_preempt_unledgered_app(rig: Rig) -> None:
    rig.adapters.app_unit.simulate_active_state("unknown", UnitStatus("active", "running", "success", 0))
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.app_unit.stop_calls == []
    assert "start" not in rig.unit.calls


def test_setup_continues_active_session_with_preempted_record(rig: Rig) -> None:
    rig.active()
    record = PreemptedApp("face", "wifi-setup")
    rig.adapters.state.write_preempted_app(record)
    rig.advance(60)
    assert rig.setup.status().mode == "active"
    assert rig.adapters.state.read_preempted_app() == record


def test_setup_connecting_preserves_qr_trial(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.active()
    rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    monkeypatch.setattr(rig.adapters.wifi_attempts, "outcome", lambda attempt_id: "attempting")
    rig.advance(10)
    assert rig.setup.status().mode == "active"
    assert rig.setup.status().screen == "connecting"


def test_setup_connected_idle_does_not_start_reader(rig: Rig) -> None:
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    rig.setup.tick()
    rig.advance(60)
    assert rig.setup.status().mode == "idle"
    assert "start" not in rig.unit.calls


def test_setup_unavailable_does_not_retry_until_restart(rig: Rig) -> None:
    rig.unit.error = WifiQrUnitMissingError()
    rig.setup.tick()
    rig.advance(6)
    rig.unit.error = None
    rig.advance(120)
    assert rig.setup.status().mode == "unavailable"
    assert "start" not in rig.unit.calls


def test_setup_attempts_stop_even_when_exit_status_read_fails(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.active()
    status = rig.unit.status
    failed = False

    def read_status() -> WifiQrUnitStatus:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("status unavailable")
        return status()

    monkeypatch.setattr(rig.unit, "status", read_status)
    rig.setup.stop("app")
    assert "stop" in rig.unit.calls
    assert rig.setup.status().mode == "idle"


def test_setup_startup_stop_budget_includes_stop_call(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def stop() -> None:
        rig.clock.monotonic_value += 4

    monkeypatch.setattr(rig.unit, "stop", stop)
    monkeypatch.setattr(rig.unit, "status", lambda: WifiQrUnitStatus("deactivating"))
    rig.setup.startup()
    assert rig.clock.monotonic() == pytest.approx(6)
    assert rig.setup.status().mode == "idle"


@pytest.mark.parametrize(
    "failure,mode",
    [
        (WifiQrUnitMissingError(), "unavailable"),
        (PolkitDeniedError("palmimo-wifi-qr.service", "start"), "backoff"),
    ],
)
def test_setup_restores_app_when_start_fails_after_preemption(
    rig: Rig,
    failure: Exception,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))

    def start() -> None:
        raise failure

    monkeypatch.setattr(rig.unit, "start", start)
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == mode
    assert rig.adapters.app_unit.stop_calls == ["face"]
    rig.setup.tick()
    assert rig.adapters.app_unit.start_calls == ["face"]
    assert rig.adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)


@pytest.mark.parametrize("failure", [AppBusyError(), AppJobInProgressStartError()])
def test_setup_retries_app_recovery_on_next_poll(rig: Rig, failure: Exception) -> None:
    rig.active()
    rig.adapters.state.write_preempted_app(PreemptedApp("face", "wifi-setup"))
    rig.unit.unit_status = WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 75)
    rig.setup.tick()
    rig.adapters.app_unit.raise_on_start = failure
    rig.setup.tick()
    assert rig.adapters.state.read_preempted_app() == PreemptedApp("face", "wifi-setup")
    rig.adapters.app_unit.raise_on_start = None
    rig.setup.tick()
    assert rig.adapters.app_unit.start_calls == ["face", "face"]
    assert rig.adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)


def test_setup_lan_clears_hold_and_later_ap_preempts_again(rig: Rig) -> None:
    rig.active()
    start_app(replace(rig.setup.apps.deps, before_start=rig.setup.before_app_start), "face", host="robot")
    assert rig.adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    rig.setup.tick()
    assert rig.adapters.state.read_preempted_app() is None
    rig.adapters.network.status = WifiStatus(ConnectionState.UNPROVISIONED, None, None)
    rig.setup.tick()
    rig.advance(6)
    assert rig.adapters.app_unit.stop_calls == ["face"]
    assert rig.setup.status().mode == "starting"


def test_setup_stopping_discards_delayed_trial_start(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def start(*args: Any) -> str:
        entered.set()
        assert release.wait(2)
        return "late-trial"

    monkeypatch.setattr(rig.adapters.wifi_attempts, "start_wifi_attempt", start)
    rig.active()
    thread = threading.Thread(target=lambda: rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"}))
    thread.start()
    try:
        assert entered.wait(2)
        assert rig.setup.status().screen == "connecting"
        rig.setup.stop("app")
        release.set()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert rig.setup.status().mode == "idle"
    assert rig.setup.status().screen is None


def test_setup_shutdown_finishes_during_slow_reader_status(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_wifi_qr_deadlines import Bus, port_for

    rig.active()
    bus = Bus()
    bus.block = "service"
    bus.unblock_on_cancel = True
    port = port_for(bus, monkeypatch)
    rig.setup.unit = port
    monitor = threading.Thread(target=rig.setup.run_forever)
    shutdown = threading.Thread(target=rig.setup.shutdown)
    monitor.start()
    try:
        assert bus.entered.wait(1)
        shutdown.start()
        assert bus.cancelled.wait(1)
    finally:
        bus.block = None
        if not shutdown.ident:
            shutdown.start()
        shutdown.join(2)
        monitor.join(2)
    assert not shutdown.is_alive()
    assert not monitor.is_alive()
    assert rig.setup.status().mode == "idle"
    port.start()
    assert bus.state == "active"


def test_setup_startup_distinguishes_not_found_load_state(rig: Rig) -> None:
    rig.unit.unit_status = WifiQrUnitStatus(load_state="not-found")
    rig.setup.startup()
    assert rig.setup.status().mode == "unavailable"


@pytest.mark.parametrize("finishes_during_poll", [False, True])
def test_setup_defers_entry_until_conflicting_app_finishes_stopping(
    rig: Rig,
    finishes_during_poll: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("deactivating", "", "success", 0))
    original_status = rig.adapters.app_unit.status
    observations: list[str] = []

    def status(name: str) -> UnitStatus:
        active = "inactive" if observations and rig.clock.monotonic() >= 6 else "deactivating"
        if rig.clock.monotonic() >= 6:
            observations.append(active)
        return UnitStatus(active, "", "success", 0)

    if finishes_during_poll:
        monkeypatch.setattr(rig.adapters.app_unit, "status", status)
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == "idle"
    assert "start" not in rig.unit.calls
    assert rig.adapters.app_unit.stop_calls == []
    monkeypatch.setattr(rig.adapters.app_unit, "status", original_status)
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("inactive", "", "success", 0))
    rig.setup.tick()
    assert rig.setup.status().mode == "starting"


@pytest.mark.parametrize("event", ["lan", "failed", "rejected_timeout"])
def test_setup_advances_screen_despite_unknown_running_app(
    rig: Rig,
    event: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.active()
    if event == "rejected_timeout":
        rig.setup.on_message({"type": "qr", "text": "poster"})
        rig.clock.monotonic_value += 6
    else:
        rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    rig.adapters.app_unit.simulate_active_state("unledgered", UnitStatus("active", "", "success", 0))
    if event == "lan":
        rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    elif event == "failed":
        monkeypatch.setattr(rig.adapters.wifi_attempts, "outcome", lambda attempt_id: "failed")
    rig.setup.tick()
    assert rig.setup.status().screen == {"lan": "connected", "failed": "failed", "rejected_timeout": "scanning"}[event]


@pytest.mark.parametrize("outcome", ["attempting", "unknown"])
def test_setup_expires_unresolved_trial_after_two_minutes(
    rig: Rig,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.active()
    monkeypatch.setattr(rig.adapters.wifi_attempts, "outcome", lambda attempt_id: outcome)
    rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    rig.advance(118)
    assert rig.setup.status().screen == "connecting"
    rig.advance(2)
    assert rig.setup.status().screen == "failed"
    assert rig.reader.messages[-1]["reason"] == "connect_failed"
    rig.advance(8)
    assert rig.setup.status().screen == "scanning"


def test_setup_defers_external_app_exit_during_run_lock_contention(rig: Rig) -> None:
    rig.active()
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "", "success", 0))
    with rig.adapters.state.lock_run():
        rig.setup.tick()
        assert rig.setup.status().mode == "active"
    rig.setup.tick()
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)


def test_setup_closes_client_created_after_stop(rig: Rig) -> None:
    def factory(message: Any, disconnected: Any) -> Reader:
        rig.setup.stop("app")
        return rig.reader

    rig.setup._factory = factory
    rig.setup.tick()
    rig.advance(6)
    assert rig.reader.closed
    assert rig.setup.status().mode == "idle"


def test_setup_releases_failed_client(rig: Rig) -> None:
    import weakref

    clients: list[weakref.ReferenceType[Reader]] = []

    def factory(message: Any, disconnected: Any) -> Reader:
        client = Reader(connect_error=True)
        clients.append(weakref.ref(client))
        return client

    rig.setup._factory = factory
    rig.setup.tick()
    rig.advance(6)
    assert clients
    assert all(client() is None for client in clients)


@pytest.mark.parametrize("boundary", ["prepare", "conflict", "socket"])
def test_setup_diagnostics_do_not_log_qr_or_password(
    rig: Rig,
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    text = "WIFI:S:home;P:secret-password;;"

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(text)

    with caplog.at_level(logging.INFO):
        if boundary == "conflict":
            rig.active()
            rig.setup.on_message({"type": "qr", "text": text})
            monkeypatch.setattr(rig.setup.apps, "conflicting_apps", fail)
            rig.setup.tick()
        else:
            if boundary == "prepare":
                monkeypatch.setattr(rig.setup.apps, "conflicting_apps", fail)
            else:
                monkeypatch.setattr(rig.reader, "connect", fail)
            rig.setup.tick()
            rig.advance(18)
    assert text not in caplog.text
    assert "secret-password" not in caplog.text


def test_setup_connected_screen_contains_url_and_reference_qr(rig: Rig) -> None:
    # Reference independently decoded with ZXing-C++: http://192.0.2.1, ECC M, no border.
    rows = """1111111011001010001111111
1000001001110000001000001
1011101001110101101011101
1011101010101011101011101
1011101011000110001011101
1000001011110110101000001
1111111010101010101111111
0000000011111011100000000
1000101110010110111111001
0110010101111001010111010
1101011110001111110101100
1010110100110100011010110
1000001100101101001001111
1110100110100111000110010
0010101111100001101101100
0001000001001010111001110
1111111101010110111111100
0000000010101001100010100
1111111010111110101011000
1000001001010101100011110
1011101011101100111110101
1011101001000110101001111
1011101000100000001000010
1000001001101011010111110
1111111011110110001000111""".splitlines()
    rig.active()
    rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    rig.setup.tick()
    assert rig.reader.messages[-1] == {
        "type": "screen",
        "state": "connected",
        "url": "http://192.0.2.1",
        "qr": {"size": 25, "rows": rows},
        "name": "robot.local",
    }


def test_setup_connected_screen_expires_after_five_minutes(rig: Rig) -> None:
    rig.active()
    rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    rig.setup.tick()
    rig.advance(298)
    assert rig.setup.status().mode == "active"
    assert rig.setup.status().screen == "connected"
    rig.advance(2)
    assert rig.setup.status().mode == "idle"
    assert rig.reader.closed


def test_setup_close_stops_connected_screen(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.active()
    rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    rig.setup.tick()
    stop = rig.unit.stop_and_wait
    modes: list[str] = []

    def stop_and_wait(*, timeout_s: float) -> None:
        modes.append(rig.setup.status().mode)
        stop(timeout_s=timeout_s)

    monkeypatch.setattr(rig.unit, "stop_and_wait", stop_and_wait)
    rig.setup.on_message({"type": "close"})
    assert modes == ["stopping"]
    assert rig.setup.status().mode == "idle"
    assert rig.reader.closed


@pytest.mark.parametrize("screen", ["scanning", "checking", "rejected", "connecting", "failed"])
def test_setup_ignores_close_outside_connected_screen(
    rig: Rig,
    screen: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from palmimo_portal.core import wifi_setup

    rig.active()

    def close() -> None:
        assert rig.setup.status().screen == screen
        rig.setup.on_message({"type": "close"})
        assert rig.setup.status().mode == "active"
        assert not rig.reader.closed

    if screen == "checking":
        parse = wifi_setup.parse_wifi_qr

        def parse_and_close(text: str) -> Any:
            close()
            return parse(text)

        monkeypatch.setattr(wifi_setup, "parse_wifi_qr", parse_and_close)
        rig.setup.on_message({"type": "qr", "text": "poster"})
        return
    if screen == "failed":

        def fail(*args: Any) -> str:
            raise RuntimeError

        monkeypatch.setattr(rig.adapters.wifi_attempts, "start_wifi_attempt", fail)
    if screen == "rejected":
        rig.setup.on_message({"type": "qr", "text": "poster"})
    elif screen in {"connecting", "failed"}:
        rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    close()


def test_setup_old_firmware_disables_reader_until_portal_restart(rig: Rig) -> None:
    rig.active()
    rig.unit.unit_status = WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 76)
    rig.setup.tick()
    assert rig.setup.status().mode == "unavailable"
    assert rig.setup.status().reason == "firmware_too_old"
    starts = rig.unit.calls.count("start")
    rig.advance(7200)
    assert rig.setup.status().mode == "unavailable"
    assert rig.unit.calls.count("start") == starts


@pytest.mark.parametrize("stop_delay,stop_fails", [(19.9, False), (20.1, False), (20.1, True)])
def test_setup_waits_for_app_release_and_retries_after_stop_timeout(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    stop_delay: float,
    stop_fails: bool,
) -> None:
    from types import SimpleNamespace

    from palmimo_portal.core import wifi_setup

    waited = 0.0
    released_at_start: list[bool] = []
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))

    def sleep(seconds: float) -> None:
        nonlocal waited
        waited += seconds
        rig.clock.monotonic_value += seconds

    def stop(name: str) -> None:
        rig.adapters.app_unit.stop_calls.append(name)
        if stop_fails:
            raise RuntimeError("unavailable")
        rig.adapters.app_unit.simulate_active_state(name, UnitStatus("deactivating", "", "success", 0))

    original_status = rig.adapters.app_unit.status

    def status(name: str) -> UnitStatus:
        if waited >= stop_delay:
            rig.adapters.app_unit.simulate_active_state(name, UnitStatus("inactive", "dead", "success", 0))
        return original_status(name)

    original_start = rig.unit.start

    def start() -> None:
        released_at_start.append(status("face").active_state == "inactive")
        original_start()

    monkeypatch.setattr(wifi_setup, "time", SimpleNamespace(monotonic=lambda: waited, sleep=sleep))
    monkeypatch.setattr(rig.adapters.app_unit, "stop", stop)
    monkeypatch.setattr(rig.adapters.app_unit, "status", status)
    monkeypatch.setattr(rig.unit, "start", start)
    rig.setup.tick()
    rig.advance(6)
    assert rig.adapters.state.read_preempted_app() == PreemptedApp("face", "wifi-setup")
    assert rig.adapters.app_unit.start_calls == []
    if stop_delay > 20:
        assert released_at_start == []
        assert 20 <= waited < stop_delay
        assert rig.setup.status().mode == "idle"
        waited = stop_delay
        rig.advance(2)
    assert released_at_start == [True]
    rig.advance(12)
    assert rig.setup.status().mode == "starting"


@pytest.mark.parametrize("failure", [TimeoutError("still stopping"), RuntimeError("D-Bus unavailable")])
def test_setup_blocks_app_until_reader_stop_is_confirmed(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.active()
    record = PreemptedApp("face", "wifi-setup")
    stop = rig.unit.stop_and_wait

    def fail(*, timeout_s: float) -> None:
        raise failure

    monkeypatch.setattr(rig.unit, "stop_and_wait", fail)
    deps = replace(rig.setup.apps.deps, before_start=rig.setup.before_app_start)
    with pytest.raises(AppBusyError):
        start_app(deps, "face", host="robot")
    assert rig.setup.status().mode == "stopping"
    assert rig.adapters.state.read_preempted_app() == record
    assert rig.adapters.app_unit.start_calls == []
    assert rig.unit.status().active_state == "active"
    assert rig.reader.closed
    rig.setup.tick()
    assert rig.setup.status().mode == "stopping"
    assert rig.adapters.state.read_preempted_app() == record
    monkeypatch.setattr(rig.unit, "stop_and_wait", stop)
    rig.setup.tick()
    assert rig.setup.status().mode == "idle"
    assert rig.unit.status().active_state == "inactive"
    start_app(deps, "face", host="robot")
    assert rig.adapters.app_unit.start_calls == ["face"]
    assert rig.adapters.state.read_preempted_app() == PreemptedApp(None, "wifi-setup", hold=True)


@pytest.mark.parametrize("event", ["connecting", "connected", "reader_exit", "hello"])
def test_setup_monitors_reader_and_deadlines_during_network_outage(
    rig: Rig, event: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    if event == "hello":
        rig.setup.tick()
        rig.advance(6)
    else:
        rig.active()
    if event in {"connecting", "connected"}:
        rig.setup.on_message({"type": "qr", "text": "WIFI:S:home;P:password;;"})
    if event == "connected":
        rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
        rig.setup.tick()
    rig.adapters.network.raise_on_get_status = AdapterUnavailableError("offline", "comitup unavailable")
    if event in {"connected", "reader_exit"}:
        stop = rig.unit.stop_and_wait
        failed = False

        def stop_after_retry(*, timeout_s: float) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise TimeoutError("still stopping")
            stop(timeout_s=timeout_s)

        monkeypatch.setattr(rig.unit, "stop_and_wait", stop_after_retry)
    if event == "connecting":
        rig.advance(120)
        assert rig.setup.status().screen == "failed"
        rig.advance(8)
        assert rig.setup.status().screen == "scanning"
        rig.unit.unit_status = WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 75)
        rig.setup.tick()
    elif event == "reader_exit":
        rig.unit.unit_status = WifiQrUnitStatus("failed", "loaded", "exit-code", 1, 76)
        rig.setup.tick()
    else:
        rig.advance(300 if event == "connected" else 30)
    if event in {"connected", "reader_exit"}:
        assert rig.setup.status().mode == "stopping"
        assert rig.adapters.state.read_preempted_app() == PreemptedApp("face", "wifi-setup")
    rig.setup.tick()
    if event == "reader_exit":
        assert rig.setup.status().reason == "firmware_too_old"
    assert rig.reader.closed
    assert rig.adapters.app_unit.start_calls == ["face"]
    assert rig.adapters.state.read_preempted_app() == (
        None if event == "connected" else PreemptedApp(None, "wifi-setup", hold=True)
    )


@pytest.mark.parametrize(
    "record", [None, PreemptedApp("face", "wifi-setup"), PreemptedApp(None, "wifi-setup", hold=True)]
)
def test_setup_connecting_starts_waiting_without_preemption(rig: Rig, record: PreemptedApp | None) -> None:
    rig.adapters.state.write_preempted_app(record)
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(4)
    assert rig.setup.status().mode == "idle"
    rig.advance(1)
    assert rig.setup.status().mode == "starting"
    rig.setup.on_message({"type": "hello", "proto": 1})
    assert rig.setup.status().screen == "waiting"
    assert rig.reader.messages == []
    assert rig.adapters.app_unit.stop_calls == []
    assert rig.adapters.state.read_preempted_app() == record


@pytest.mark.parametrize("devices,enters", [(["camera"], False), (["motor_display"], False), (["audio"], True)])
def test_setup_notice_respects_running_app_devices(rig: Rig, devices: list[str], enters: bool) -> None:
    path = rig.setup.apps.deps.apps_job_ctx.app_dir("face") / "palmimo.toml"
    path.write_text(path.read_text().replace('["camera"]', str(devices).replace("'", '"')))
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == ("starting" if enters else "idle")
    assert rig.adapters.app_unit.stop_calls == []


def test_setup_web_attempt_resets_notice_timer(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(4)
    manager = type(rig.adapters.wifi_attempts)
    monkeypatch.setattr(manager, "in_progress", property(lambda self: True))
    rig.advance(20)
    assert rig.setup.status().mode == "idle"
    monkeypatch.setattr(manager, "in_progress", property(lambda self: False))
    rig.advance(4)
    assert rig.setup.status().mode == "idle"
    rig.advance(1)
    assert rig.setup.status().mode == "starting"


@pytest.mark.parametrize("hello_before_ap", [True, False])
def test_setup_waiting_advances_to_scanning_after_five_ap_seconds(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hello_before_ap: bool
) -> None:
    monkeypatch.setattr(rig.adapters.network, "has_known_networks", lambda: True)
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(5)
    if hello_before_ap:
        rig.setup.on_message({"type": "hello", "proto": 1})
        assert rig.setup.status().screen == "waiting"
    rig.adapters.network.status = WifiStatus(ConnectionState.UNPROVISIONED, None, None)
    rig.setup.tick()
    rig.advance(4)
    assert rig.setup.status().screen == ("waiting" if hello_before_ap else None)
    rig.advance(1)
    if not hello_before_ap:
        rig.setup.on_message({"type": "hello", "proto": 1})
    assert rig.reader.messages[-1] == {"type": "screen", "state": "scanning"}


def test_setup_known_network_waits_sixty_ap_seconds_before_preempting(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rig.adapters.network, "has_known_networks", lambda: True)
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.setup.tick()
    rig.advance(59)
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.app_unit.stop_calls == []
    rig.advance(1)
    assert rig.setup.status().mode == "starting"
    assert rig.adapters.app_unit.stop_calls == ["face"]
    rig.setup.on_message({"type": "hello", "proto": 1})
    assert rig.reader.messages == [{"type": "screen", "state": "scanning"}]


@pytest.mark.parametrize(
    "state,ip,nm",
    [(ConnectionState.CONNECTED, None, 100), (ConnectionState.CONNECTED, "192.0.2.1", 70), (None, None, 100)],
)
def test_setup_notice_timer_pauses_without_network_observation(
    rig: Rig, state: ConnectionState | None, ip: str | None, nm: int
) -> None:
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(3)
    if state is None:
        rig.adapters.network.raise_on_get_status = AdapterUnavailableError("offline", "no bus")
    else:
        rig.adapters.network.status = WifiStatus(state, "home", ip)
        rig.adapters.wifi_profiles.device = WifiDeviceStatus(nm, None)
    rig.advance(20)
    assert rig.setup.status().mode == "idle"
    rig.adapters.network.raise_on_get_status = None
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.advance(1)
    assert rig.setup.status().mode == "idle"
    rig.advance(1)
    assert rig.setup.status().mode == "starting"


@pytest.mark.parametrize("phase", ["starting", "waiting", "hello_during_lan"])
def test_setup_lan_return_stops_notice_without_success_screen(
    rig: Rig, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(5)
    assert rig.setup.status().mode == "starting"
    if phase == "waiting":
        rig.setup.on_message({"type": "hello", "proto": 1})
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    if phase == "hello_during_lan":
        monkeypatch.setattr(rig.setup.apps, "clear_hold", lambda: rig.setup.on_message({"type": "hello", "proto": 1}))
    rig.setup.tick()
    assert rig.setup.status().mode == "idle"
    assert rig.unit.status().active_state == "inactive"
    rig.setup.on_message({"type": "hello", "proto": 1})
    assert rig.reader.messages == []


def test_setup_notice_rechecks_apps_under_run_lock(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    original = rig.adapters.state.lock_run

    @contextmanager
    def lock_run(timeout_s: float = 0.0) -> Iterator[None]:
        with original(timeout_s):
            rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
            yield

    monkeypatch.setattr(rig.adapters.state, "lock_run", lock_run)
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(5)
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.app_unit.stop_calls == []
    assert "start" not in rig.unit.calls


def test_setup_firmware_recovery_blocks_notice(rig: Rig) -> None:
    record = PreemptedApp("face", "display-firmware")
    rig.adapters.state.write_preempted_app(record)
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == "idle"
    assert "start" not in rig.unit.calls
    assert rig.adapters.state.read_preempted_app() == record


@pytest.mark.parametrize("exit", ["web", "outside", "disconnect"])
def test_setup_waiting_preserves_exit_conditions(rig: Rig, exit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    disconnects: list[Any] = []

    def factory(message: Any, disconnect: Any) -> Reader:
        disconnects.append(disconnect)
        return rig.reader

    monkeypatch.setattr(rig.setup, "_factory", factory)
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(5)
    rig.setup.on_message({"type": "hello", "proto": 1})
    assert rig.setup.status().screen == "waiting"
    if exit == "web":
        start_app(replace(rig.setup.apps.deps, before_start=rig.setup.before_app_start), "face", host="robot")
    elif exit == "outside":
        rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
        rig.setup.tick()
    else:
        disconnects[0]()
        rig.setup.tick()
    assert rig.setup.status().mode == ("backoff" if exit == "disconnect" else "idle")
    assert rig.setup.status().screen is None


def test_setup_live_entry_rechecks_short_delay_before_preempting(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rig.adapters.network, "has_known_networks", lambda: True)
    original = rig.adapters.state.lock_run

    @contextmanager
    def lock_run(timeout_s: float = 0.0) -> Iterator[None]:
        with original(timeout_s):
            rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
            yield

    monkeypatch.setattr(rig.adapters.state, "lock_run", lock_run)
    rig.setup.tick()
    rig.advance(6)
    assert rig.setup.status().mode == "idle"
    assert rig.adapters.app_unit.stop_calls == []
    assert "start" not in rig.unit.calls


def test_setup_lan_return_resets_notice_timer(rig: Rig) -> None:
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(4)
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTED, "home", "192.0.2.1")
    rig.setup.tick()
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.advance(4)
    assert rig.setup.status().mode == "idle"
    rig.advance(1)
    assert rig.setup.status().mode == "starting"


@pytest.mark.parametrize("interruption", [ConnectionState.CONNECTING, None])
def test_setup_waiting_ap_timer_counts_only_observed_ap_intervals(
    rig: Rig, interruption: ConnectionState | None
) -> None:
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(5)
    rig.setup.on_message({"type": "hello", "proto": 1})
    rig.adapters.network.status = WifiStatus(ConnectionState.UNPROVISIONED, None, None)
    rig.setup.tick()
    rig.advance(3)
    if interruption is None:
        rig.adapters.network.raise_on_get_status = AdapterUnavailableError("offline", "no bus")
    else:
        rig.adapters.network.status = WifiStatus(interruption, None, None)
    rig.advance(20)
    assert rig.setup.status().screen == "waiting"
    rig.adapters.network.raise_on_get_status = None
    rig.adapters.network.status = WifiStatus(ConnectionState.UNPROVISIONED, None, None)
    rig.setup.tick()
    rig.advance(1)
    assert rig.setup.status().screen == "waiting"
    rig.advance(1 if interruption is None else 4)
    assert rig.setup.status().screen == "scanning"


@pytest.mark.parametrize("device", ["camera", "motor_display"])
def test_setup_waiting_stops_before_scanning_when_external_app_starts(rig: Rig, device: str) -> None:
    path = rig.setup.apps.deps.apps_job_ctx.app_dir("face") / "palmimo.toml"
    path.write_text(path.read_text().replace('["camera"]', f'["{device}"]'))
    rig.adapters.network.status = WifiStatus(ConnectionState.CONNECTING, None, None)
    rig.setup.tick()
    rig.advance(5)
    rig.setup.on_message({"type": "hello", "proto": 1})
    assert rig.setup.status().screen == "waiting"
    rig.adapters.network.status = WifiStatus(ConnectionState.UNPROVISIONED, None, None)
    rig.setup.tick()
    rig.advance(4)
    rig.adapters.app_unit.simulate_active_state("face", UnitStatus("active", "running", "success", 0))
    rig.advance(1)
    assert rig.setup.status().mode == "idle"
    assert rig.reader.closed
    assert not any(message["state"] == "scanning" for message in rig.reader.messages)
