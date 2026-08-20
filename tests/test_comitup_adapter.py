"""CI-safe tests for :mod:`palmimo_portal.adapters.comitup`.

No real D-Bus connection: the mapping/parsing functions are pure, and
:meth:`ComitupNetworkPort._call` -- the seam between sync Port methods and
the D-Bus transport -- is stubbed by subclassing. Tests needing a real
comitup service live in ``test_comitup_live.py``, gated behind ``--live``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface

from palmimo_portal.adapters.comitup import (
    ComitupNetworkPort,
    map_comitup_state,
    parse_access_points,
    parse_security,
    parse_signal_strength,
)
from palmimo_portal.ports import AdapterUnavailableError, ConnectionState, NotConnectedError, WifiAttempt, WifiNetwork
from palmimo_portal.testing.fakes import FakeStateStore


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("CONNECTED", ConnectionState.CONNECTED),
        ("CONNECTING", ConnectionState.CONNECTING),
        ("HOTSPOT", ConnectionState.UNPROVISIONED),
        ("SOMETHING_FUTURE_COMITUP_ADDS", ConnectionState.UNPROVISIONED),
    ],
)
def test_map_comitup_state(state: str, expected: ConnectionState) -> None:
    assert map_comitup_state(state) is expected


@pytest.mark.parametrize(
    ("strength", "expected"),
    [("72", 72), ("0", 0), ("", 0), ("not-a-number", 0)],
)
def test_parse_signal_strength(strength: str, expected: int) -> None:
    assert parse_signal_strength(strength) == expected


@pytest.mark.parametrize(
    ("security", "expected"),
    [
        ("WPA2", True),
        ("wpa2-psk", True),
        ("", False),
        ("none", False),
        ("None", False),
        ("open", False),
        ("--", False),
    ],
)
def test_parse_security(security: str, expected: bool) -> None:
    assert parse_security(security) is expected


def test_parse_access_points_maps_every_field() -> None:
    records = [{"ssid": "HomeNet", "strength": "88", "security": "WPA2"}]

    assert parse_access_points(records) == [WifiNetwork(ssid="HomeNet", signal=88, secured=True)]


def test_parse_access_points_drops_entries_with_no_ssid() -> None:
    records = [{"ssid": "", "strength": "50", "security": ""}, {"ssid": "Real", "strength": "10", "security": ""}]

    result = parse_access_points(records)

    assert [n.ssid for n in result] == ["Real"]


class _StubbedNetworkPort(ComitupNetworkPort):
    """A :class:`ComitupNetworkPort` whose ``_call`` is entirely scripted -- no bus."""

    def __init__(self, script: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script: dict[str, Any] = script or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def _call(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        self.calls.append((member, args))
        outcome = self.script[member]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_get_status_maps_connected_state_and_reads_the_ssid(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _StubbedNetworkPort({"state": ["CONNECTED", "jizaiten_EXT"]})
    monkeypatch.setattr("palmimo_portal.adapters.comitup._read_wlan_ipv4", lambda iface: "192.0.2.5")

    status = port.get_status()

    assert status.state is ConnectionState.CONNECTED
    assert status.ssid == "jizaiten_EXT"
    assert status.ip_address == "192.0.2.5"


def test_get_status_reports_no_ip_when_not_connected() -> None:
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]})

    status = port.get_status()

    assert status.state is ConnectionState.UNPROVISIONED
    assert status.ssid is None
    assert status.ip_address is None


def test_get_status_does_not_read_ip_when_hotspot(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_iface: str) -> str | None:
        raise AssertionError("must not read an interface IP while not connected")

    monkeypatch.setattr("palmimo_portal.adapters.comitup._read_wlan_ipv4", fail)
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]})

    port.get_status()  # must not raise


def test_list_networks_parses_the_scan_result() -> None:
    port = _StubbedNetworkPort({"access_points": [{"ssid": "Net1", "strength": "40", "security": "WPA2"}]})

    networks = port.list_networks()

    assert networks == [WifiNetwork(ssid="Net1", signal=40, secured=True)]


def test_connect_calls_comitup_with_ssid_and_psk() -> None:
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"], "connect": None})

    port.connect("HomeNet", "hunter2")

    assert port.calls[-1] == ("connect", ("HomeNet", "hunter2"))


def test_forget_current_calls_delete_connection() -> None:
    port = _StubbedNetworkPort({"state": ["CONNECTED", "HomeNet"], "delete_connection": None})
    port.get_status()  # an earlier observation -- forget_current must still poll fresh itself

    port.forget_current()

    assert port.calls[-1] == ("delete_connection", ())


def test_forget_current_polls_state_first_when_nothing_observed_yet() -> None:
    port = _StubbedNetworkPort({"state": ["CONNECTED", "HomeNet"], "delete_connection": None})

    port.forget_current()

    assert port.calls == [("state", ()), ("delete_connection", ())]


def test_forget_current_logs_the_ssid_from_the_last_observed_status(caplog: pytest.LogCaptureFixture) -> None:
    port = _StubbedNetworkPort({"state": ["CONNECTED", "HomeNet"], "delete_connection": None})
    port.get_status()  # an earlier observation -- forget_current must still poll fresh itself

    with caplog.at_level(logging.INFO, logger="palmimo_portal"):
        port.forget_current()

    assert "comitup: forgetting current network 'HomeNet'" in caplog.text


def test_forget_current_raises_not_connected_error_while_hotspot() -> None:
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]})

    with pytest.raises(NotConnectedError):
        port.forget_current()

    assert ("delete_connection", ()) not in port.calls


def test_forget_current_logs_a_warning_when_the_marker_unlink_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A marker path that is itself a directory makes unlink() raise
    # IsADirectoryError (an OSError) -- _clear_known's own except clause
    # must log this, not let it escape and fail the whole forget_current()
    # call the caller (`in_memory_known` is still cleared either way, so a
    # provisioning-state check after this stays correct even though the
    # on-disk marker itself was not removed).
    marker = tmp_path / "known"
    marker.mkdir()
    port = _StubbedNetworkPort(
        {"state": ["CONNECTED", "HomeNet"], "delete_connection": None}, known_network_marker=marker
    )
    port.get_status()
    assert port.has_known_networks() is True

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        port.forget_current()  # must not raise

    assert "failed to remove known-network marker" in caplog.text
    assert str(marker) in caplog.text
    # In-memory state is cleared regardless of the on-disk marker's fate --
    # a poll-interval's worth of "known" over-reporting is the documented
    # trade-off (see _clear_known's docstring), but in-memory itself must
    # not silently stay stuck "known" too.
    assert port.has_known_networks() is False


def test_forget_current_ignores_a_stale_cached_connected_state_when_fresh_state_is_hotspot() -> None:
    # The cache (e.g. left over from an earlier get_status() poll) says
    # CONNECTED, but a fresh state() read -- what forget_current must
    # actually decide from -- says HOTSPOT. Deciding from the stale cache
    # here would wrongly call delete_connection() while comitup is in
    # HOTSPOT, deleting comitup's own hotspot profile (see the module
    # docstring's "Never decide from a cached state" section) instead of
    # raising NotConnectedError as it must.
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]})
    port._last_logged = ("CONNECTED", "OldNet")  # stale -- must be ignored

    with pytest.raises(NotConnectedError):
        port.forget_current()

    assert ("delete_connection", ()) not in port.calls


def test_connect_does_not_forget_first_when_not_currently_connected() -> None:
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"], "connect": None})
    port.get_status()  # an earlier observation -- connect must still poll fresh itself

    port.connect("HomeNet", "hunter2")

    assert port.calls[-1] == ("connect", ("HomeNet", "hunter2"))
    assert ("delete_connection", ()) not in port.calls


def test_connect_forgets_the_current_network_first_when_already_connected() -> None:
    port = _StubbedNetworkPort({"state": ["CONNECTED", "OldNet"], "delete_connection": None, "connect": None})

    port.connect("NewNet", "hunter2")

    # A single fresh state() read, then forget-then-connect, in that order
    # -- see the module docstring's "Never decide from a cached state"
    # section for why this must be exactly one state() call rather than one
    # cached from an earlier get_status() and a second, redundant fresh one.
    assert port.calls == [
        ("state", ()),
        ("delete_connection", ()),
        ("connect", ("NewNet", "hunter2")),
    ]


def test_connect_ignores_a_stale_cached_connected_state_when_fresh_state_is_hotspot() -> None:
    # The cache (e.g. left over from an earlier get_status() poll) says
    # CONNECTED, but a fresh state() read -- what this method must actually
    # decide from -- says HOTSPOT. Deciding from the stale cache here would
    # wrongly call delete_connection() while comitup is in HOTSPOT, which
    # deletes comitup's own hotspot profile (see the module docstring).
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"], "connect": None})
    port._last_logged = ("CONNECTED", "OldNet")  # stale -- must be ignored

    port.connect("NewNet", "hunter2")

    assert ("delete_connection", ()) not in port.calls
    assert port.calls == [("state", ()), ("connect", ("NewNet", "hunter2"))]


def test_connect_while_connected_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    port = _StubbedNetworkPort({"state": ["CONNECTED", "OldNet"], "delete_connection": None, "connect": None})
    port.get_status()

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        port.connect("NewNet", "hunter2")

    assert "connect requested while CONNECTED to 'OldNet'; forgetting it first" in caplog.text


def test_connect_polls_state_first_when_nothing_observed_yet_before_deciding_to_forget() -> None:
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"], "connect": None})

    port.connect("HomeNet", "hunter2")

    assert port.calls == [("state", ()), ("connect", ("HomeNet", "hunter2"))]


def test_forget_current_clears_the_known_marker_and_in_memory_flag(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    port = _StubbedNetworkPort(
        {"state": ["CONNECTED", "HomeNet"], "delete_connection": None}, known_network_marker=marker
    )
    port.get_status()  # observing CONNECTED marks the network known
    assert port.has_known_networks() is True
    assert marker.is_file()

    port.forget_current()

    assert port.has_known_networks() is False
    assert not marker.is_file()


def test_forget_current_then_later_connected_observation_remarks_known(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    port = _StubbedNetworkPort(
        {"state": ["CONNECTED", "HomeNet"], "delete_connection": None}, known_network_marker=marker
    )
    port.get_status()
    port.forget_current()
    assert port.has_known_networks() is False

    # comitup still had another known profile and reconnected to it on its
    # own -- the next CONNECTED observation must re-create the marker.
    port.script["state"] = ["CONNECTED", "OtherNet"]
    port.get_status()

    assert port.has_known_networks() is True
    assert marker.is_file()


def test_connect_while_connected_clears_known_marker_before_reconnecting(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    port = _StubbedNetworkPort(
        {
            "state": ["CONNECTED", "OldNet"],
            "delete_connection": None,
            "connect": AdapterUnavailableError("network_backend_unavailable", "boom"),
        },
        known_network_marker=marker,
    )
    port.get_status()  # observing CONNECTED marks OldNet known
    assert port.has_known_networks() is True

    # The reconnect attempt itself fails -- but delete_connection() already
    # succeeded, so the known-network marker must already be cleared by the
    # time connect() raises, not left claiming a network that is gone.
    with pytest.raises(AdapterUnavailableError):
        port.connect("NewNet", "hunter2")

    assert port.has_known_networks() is False
    assert not marker.is_file()


def test_connect_while_connected_remarks_known_after_a_successful_reconnect(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    port = _StubbedNetworkPort(
        {"state": ["CONNECTED", "OldNet"], "delete_connection": None, "connect": None}, known_network_marker=marker
    )
    port.get_status()
    assert port.has_known_networks() is True

    port.connect("NewNet", "hunter2")

    assert port.has_known_networks() is True
    assert marker.is_file()


def test_has_known_networks_is_false_with_no_marker_and_nothing_observed(tmp_path: Path) -> None:
    port = _StubbedNetworkPort({}, known_network_marker=tmp_path / "known")

    assert port.has_known_networks() is False


def test_has_known_networks_becomes_true_after_connecting_state_observed(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    port = _StubbedNetworkPort({"state": ["CONNECTING", "HomeNet"]}, known_network_marker=marker)

    port.get_status()

    assert port.has_known_networks() is True
    assert marker.is_file()


def test_has_known_networks_becomes_true_after_a_successful_connect_call(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"], "connect": None}, known_network_marker=marker)

    port.connect("HomeNet", "hunter2")

    assert port.has_known_networks() is True
    assert marker.is_file()


def test_has_known_networks_stays_false_when_connect_raises(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    port = _StubbedNetworkPort(
        {"connect": AdapterUnavailableError("network_backend_unavailable", "boom")}, known_network_marker=marker
    )

    with pytest.raises(AdapterUnavailableError):
        port.connect("HomeNet", "hunter2")

    assert port.has_known_networks() is False
    assert not marker.is_file()


def test_has_known_networks_survives_a_fresh_instance_via_the_marker_file(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    first = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"], "connect": None}, known_network_marker=marker)
    first.connect("HomeNet", "hunter2")

    second = _StubbedNetworkPort({}, known_network_marker=marker)

    assert second.has_known_networks() is True


def test_has_known_networks_does_not_mark_the_hotspot_state_as_known(tmp_path: Path) -> None:
    marker = tmp_path / "known"
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]}, known_network_marker=marker)

    port.get_status()

    assert port.has_known_networks() is False
    assert not marker.is_file()


def test_get_status_does_not_log_a_transition_on_the_first_poll(caplog: pytest.LogCaptureFixture) -> None:
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]})

    with caplog.at_level(logging.INFO, logger="palmimo_portal"):
        port.get_status()

    assert "network state:" not in caplog.text


def test_get_status_logs_the_first_observed_state_at_info(caplog: pytest.LogCaptureFixture) -> None:
    # comitup keeps no journal record of its own state at all, so without
    # this the boot-time state (e.g. CONNECTED to the home network before
    # this process ever polled) is invisible in journalctl -- only later
    # *transitions* are logged otherwise.
    port = _StubbedNetworkPort({"state": ["CONNECTED", "HomeNet"]})

    with caplog.at_level(logging.INFO, logger="palmimo_portal"):
        port.get_status()

    assert "network state observed: CONNECTED (HomeNet)" in caplog.text


def test_get_status_does_not_log_the_first_observation_again_on_the_second_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    port = _StubbedNetworkPort({"state": ["CONNECTED", "HomeNet"]})
    port.get_status()  # first observation, already logged

    with caplog.at_level(logging.INFO, logger="palmimo_portal"):
        port.get_status()

    assert "network state observed:" not in caplog.text


def test_get_status_logs_an_info_line_when_the_state_changes(caplog: pytest.LogCaptureFixture) -> None:
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]})
    port.get_status()  # first observation -- no log yet
    port.script["state"] = ["CONNECTING", "HomeNet"]

    with caplog.at_level(logging.INFO, logger="palmimo_portal"):
        port.get_status()

    assert "network state: HOTSPOT -> CONNECTING (HomeNet)" in caplog.text


def test_get_status_does_not_log_when_the_state_is_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    port = _StubbedNetworkPort({"state": ["CONNECTED", "HomeNet"]})
    port.get_status()

    with caplog.at_level(logging.INFO, logger="palmimo_portal"):
        port.get_status()

    assert "network state:" not in caplog.text


def test_get_status_raises_adapter_unavailable_when_the_dbus_call_fails_twice() -> None:
    port = _StubbedNetworkPort({"state": TimeoutError("no reply")})

    with pytest.raises(AdapterUnavailableError):
        port.get_status()


def test_list_networks_raises_adapter_unavailable_on_persistent_failure() -> None:
    port = _StubbedNetworkPort({"access_points": ConnectionError("bus gone")})

    with pytest.raises(AdapterUnavailableError):
        port.list_networks()


class _FlakyOnceNetworkPort(ComitupNetworkPort):
    """Fails the first ``_call`` for a member, then succeeds -- to exercise the retry path.

    Overrides ``_call`` directly (the same CI-safe seam every other test in
    this file stubs), which bypasses the real ``_connect``/``_disconnect``/
    ``_lock`` machinery entirely -- so this only proves *retry-once*
    happens. The identity-guarded disconnect behavior those real methods
    add is proven separately below, against a real (if fake-transport)
    ``_connect``/``_disconnect`` pair via ``_open_bus``.
    """

    def __init__(self, results: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._results = results
        self.attempts: dict[str, int] = {}

    async def _call(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        self.attempts[member] = self.attempts.get(member, 0) + 1
        if self.attempts[member] == 1:
            raise ConnectionError("comitup restarted mid-call")
        return self._results[member]


def test_call_resilient_reconnects_and_retries_once_after_a_dropped_connection() -> None:
    port = _FlakyOnceNetworkPort({"state": ["CONNECTED", "HomeNet"]})

    status = port.get_status()

    assert status.state is ConnectionState.CONNECTED
    assert port.attempts["state"] == 2


class _AlwaysFailsNetworkPort(ComitupNetworkPort):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attempts = 0

    async def _call(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        self.attempts += 1
        raise ConnectionError("comitup is down")


def test_call_resilient_gives_up_after_one_retry() -> None:
    port = _AlwaysFailsNetworkPort()

    with pytest.raises(AdapterUnavailableError):
        port.get_status()

    assert port.attempts == 2  # the original attempt, plus exactly one retry


def test_transition_to_hotspot_marks_a_pending_attempt_failed() -> None:
    state = FakeStateStore()
    state.write_last_wifi_attempt(WifiAttempt(ssid="HomeNet", result="attempting", timestamp=1.0))
    port = _StubbedNetworkPort({"state": ["CONNECTING", "HomeNet"]}, state_store=state)
    port.get_status()  # first observation: CONNECTING, no transition yet
    port.script["state"] = ["HOTSPOT", "jizaiten-ap"]

    port.get_status()  # transition CONNECTING -> HOTSPOT: the attempt failed

    updated = state.read_last_wifi_attempt()
    assert updated is not None
    assert updated.result == "failed"
    assert updated.ssid == "HomeNet"  # the attempt's own ssid, not the AP name
    # comitup reports its own hotspot's broadcast name ("jizaiten-ap") as the
    # connection name even in HOTSPOT state (see the module docstring) -- that
    # is not a network Palmimo "joined", so the *persisted* record must not
    # carry it as observed_connection_name (confirms resolve_attempt's fix is
    # not undone by this adapter re-introducing the name some other way).
    assert updated.observed_connection_name is None


def test_transition_to_connected_marks_a_pending_attempt_connected() -> None:
    state = FakeStateStore()
    state.write_last_wifi_attempt(WifiAttempt(ssid="HomeNet", result="attempting", timestamp=1.0))
    port = _StubbedNetworkPort({"state": ["CONNECTING", "HomeNet"]}, state_store=state)
    port.get_status()
    port.script["state"] = ["CONNECTED", "HomeNet"]

    port.get_status()

    updated = state.read_last_wifi_attempt()
    assert updated is not None
    assert updated.result == "connected"


def test_transition_does_not_touch_last_attempt_when_none_is_pending() -> None:
    state = FakeStateStore()  # no attempt recorded at all
    port = _StubbedNetworkPort({"state": ["CONNECTING", "HomeNet"]}, state_store=state)
    port.get_status()
    port.script["state"] = ["CONNECTED", "HomeNet"]

    port.get_status()  # must not raise, and must not fabricate an attempt

    assert state.read_last_wifi_attempt() is None


def test_transition_does_not_overwrite_an_attempt_that_is_not_attempting() -> None:
    # Idempotence: once resolved (or if it was never "attempting" to begin
    # with -- e.g. a stale "connected" from a previous session), a later
    # transition must not touch it again.
    state = FakeStateStore()
    state.write_last_wifi_attempt(WifiAttempt(ssid="Other", result="connected", timestamp=1.0))
    port = _StubbedNetworkPort({"state": ["CONNECTING", "HomeNet"]}, state_store=state)
    port.get_status()
    port.script["state"] = ["HOTSPOT", "jizaiten-ap"]

    port.get_status()

    updated = state.read_last_wifi_attempt()
    assert updated is not None
    assert updated.ssid == "Other"
    assert updated.result == "connected"


def test_repeated_identical_transitions_update_last_attempt_only_once() -> None:
    state = FakeStateStore()
    state.write_last_wifi_attempt(WifiAttempt(ssid="HomeNet", result="attempting", timestamp=1.0))
    port = _StubbedNetworkPort({"state": ["CONNECTING", "HomeNet"]}, state_store=state)
    port.get_status()
    port.script["state"] = ["CONNECTED", "HomeNet"]
    port.get_status()  # resolves to "connected"
    first_timestamp = state.read_last_wifi_attempt()
    assert first_timestamp is not None

    port.get_status()  # same state again -- not a transition at all

    second = state.read_last_wifi_attempt()
    assert second is not None
    assert second.timestamp == first_timestamp.timestamp  # untouched the second time


def test_first_observation_resolves_a_pending_attempt_after_a_restart() -> None:
    # Simulates a process restart: the attempt was written before the
    # restart, and the *first* status poll of a fresh instance lands
    # directly on a settled state -- there is no "previous" state to
    # detect a transition against, but it must resolve all the same (see
    # palmimo_portal.core.wifi_attempt's module docstring).
    state = FakeStateStore()
    state.write_last_wifi_attempt(WifiAttempt(ssid="HomeNet", result="attempting", timestamp=1.0))
    port = _StubbedNetworkPort({"state": ["CONNECTED", "HomeNet"]}, state_store=state)

    port.get_status()  # first observation ever, already CONNECTED

    updated = state.read_last_wifi_attempt()
    assert updated is not None
    assert updated.result == "connected"


def test_repeated_unchanged_hotspot_still_resolves_once_an_attempt_becomes_pending() -> None:
    # A "round trip the poller never saw" case: comitup could have gone to
    # CONNECTING and back to HOTSPOT between two polls that both observed
    # HOTSPOT -- a change-detecting caller would never notice a transition
    # to react to. Calling resolve_attempt on every observation, not only
    # a detected change, is what still resolves the attempt here.
    state = FakeStateStore()
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]}, state_store=state)
    port.get_status()  # first observation, nothing pending yet

    state.write_last_wifi_attempt(WifiAttempt(ssid="HomeNet", result="attempting", timestamp=1.0))

    port.get_status()  # same HOTSPOT state again -- not a detected transition

    updated = state.read_last_wifi_attempt()
    assert updated is not None
    assert updated.result == "failed"


def test_grace_period_protects_a_freshly_written_attempt_from_an_already_hotspot_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = 100_000.0
    monkeypatch.setattr("palmimo_portal.adapters.comitup.time.time", lambda: fixed_now)
    state = FakeStateStore()
    state.write_last_wifi_attempt(WifiAttempt(ssid="HomeNet", result="attempting", timestamp=fixed_now - 1))
    port = _StubbedNetworkPort({"state": ["HOTSPOT", "jizaiten-ap"]}, state_store=state)

    port.get_status()  # first observation, already HOTSPOT, but the attempt is only 1s old

    updated = state.read_last_wifi_attempt()
    assert updated is not None
    assert updated.result == "attempting"  # untouched -- still within the grace period


def test_connected_transition_to_a_different_network_past_the_grace_period_resolves_failed() -> None:
    # attempt.timestamp=1.0 is far in the past relative to the real
    # time.time() this test does not mock, so this is well past
    # GRACE_PERIOD_SECONDS -- comitup settled on a network other than the
    # one requested, and that settling is final: resolve_attempt's
    # reconfigure-race rule (see palmimo_portal.core.wifi_attempt) resolves
    # this as "failed", not "connected".
    state = FakeStateStore()
    state.write_last_wifi_attempt(WifiAttempt(ssid="HomeNet", result="attempting", timestamp=1.0))
    port = _StubbedNetworkPort({"state": ["CONNECTING", "HomeNet"]}, state_store=state)
    port.get_status()
    port.script["state"] = ["CONNECTED", "SomeOtherKnownNetwork"]

    port.get_status()

    updated = state.read_last_wifi_attempt()
    assert updated is not None
    assert updated.result == "failed"
    assert updated.ssid == "HomeNet"  # what the client asked to connect to
    assert updated.observed_connection_name == "SomeOtherKnownNetwork"  # what comitup actually settled on


def test_connected_transition_to_a_different_network_within_the_grace_period_does_not_resolve_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulates the reconfigure race: right after POST /wifi/connect, the
    # *old* network can still observably be CONNECTED for a moment before
    # comitup actually starts the new attempt. That must not be read as
    # "connected" (to the wrong network) nor as "failed" -- it is simply not
    # resolved yet.
    fixed_now = 100_000.0
    monkeypatch.setattr("palmimo_portal.adapters.comitup.time.time", lambda: fixed_now)
    state = FakeStateStore()
    state.write_last_wifi_attempt(WifiAttempt(ssid="HomeNet", result="attempting", timestamp=fixed_now - 1))
    port = _StubbedNetworkPort({"state": ["CONNECTED", "OldNet"]}, state_store=state)

    port.get_status()  # first observation: still CONNECTED to the old network

    updated = state.read_last_wifi_attempt()
    assert updated is not None
    assert updated.result == "attempting"  # untouched -- still within the grace period


def test_get_status_does_not_raise_when_persisting_the_resolved_attempt_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write failure while resolving a pending attempt must not turn a successful status read into a failed one."""

    class _RaisingOnWriteStateStore(FakeStateStore):
        def write_last_wifi_attempt(self, attempt: WifiAttempt) -> None:
            raise OSError("disk full")

    state = _RaisingOnWriteStateStore()
    state._last_attempt = WifiAttempt(ssid="HomeNet", result="attempting", timestamp=1.0)
    port = _StubbedNetworkPort({"state": ["CONNECTED", "HomeNet"]}, state_store=state)

    with caplog.at_level(logging.ERROR):
        status = port.get_status()  # must not raise

    assert status.state is ConnectionState.CONNECTED
    assert "failed to persist resolved last_wifi_attempt" in caplog.text


def test_no_state_store_is_a_no_op() -> None:
    # The default construction (no state_store given) must not raise --
    # covers any call site that still constructs ComitupNetworkPort without
    # one (defense in depth; wiring.py always passes one for the real adapter).
    port = _StubbedNetworkPort({"state": ["CONNECTING", "HomeNet"]})
    port.get_status()
    port.script["state"] = ["CONNECTED", "HomeNet"]

    port.get_status()  # must not raise


class _FakeBus:
    """Stands in for a :class:`dbus_fast.aio.MessageBus`: only tracks ``disconnect()`` calls."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.disconnect_calls = 0

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def __repr__(self) -> str:
        return f"_FakeBus({self.label!r})"


class _FakeInterface:
    """Stands in for a :class:`~dbus_fast.aio.proxy_object.ProxyInterface`.

    ``healthy=False`` makes every ``call_state()`` raise, simulating a
    dropped bus every real call would observe as a transport failure.
    """

    def __init__(self, *, healthy: bool) -> None:
        self.healthy = healthy
        self.call_count = 0

    async def call_state(self) -> Any:
        self.call_count += 1
        if not self.healthy:
            raise ConnectionError("bus dropped")
        return ("CONNECTED", "HomeNet")


class _OpenBusScriptedNetworkPort(ComitupNetworkPort):
    """Real ``_connect``/``_disconnect``/``_call``/``_call_resilient``/``_lock`` -- only ``_open_bus`` is scripted."""

    def __init__(self, open_bus_script: list[Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._open_bus_script = list(open_bus_script)
        self.open_bus_calls = 0

    async def _open_bus(self) -> tuple[Any, Any]:
        self.open_bus_calls += 1
        outcome = self._open_bus_script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_concurrent_calls_over_a_dropped_bus_reconnect_exactly_once() -> None:
    # Pre-seed an already-connected, but now-dropped, bus/interface -- the
    # scenario comitup restarting mid-session produces. Only one fresh
    # (bus, interface) pair is on the _open_bus script: if serialization via
    # _lock did not hold, a second concurrent reconnect attempt would starve
    # the script (IndexError) instead of reusing the first reconnect.
    old_bus = _FakeBus("old")
    new_bus = _FakeBus("new")
    new_interface = _FakeInterface(healthy=True)
    port = _OpenBusScriptedNetworkPort([(new_bus, new_interface)])
    port._bus = cast(MessageBus, old_bus)
    port._interface = cast(ProxyInterface, _FakeInterface(healthy=False))

    first, second = await asyncio.gather(
        port._call_resilient("state", (), 5.0),
        port._call_resilient("state", (), 5.0),
    )

    assert first == ("CONNECTED", "HomeNet")
    assert second == ("CONNECTED", "HomeNet")
    assert port.open_bus_calls == 1
    assert old_bus.disconnect_calls == 1
    assert new_bus.disconnect_calls == 0
    assert port._bus is new_bus


async def test_a_stale_disconnect_does_not_tear_down_a_bus_another_call_already_replaced() -> None:
    # Simulates the interleaving _disconnect's identity guard exists for: a
    # failing call captured a reference to the *old* bus before another,
    # concurrent call already disconnected-and-reconnected past it. The
    # stale call's own (late) cleanup attempt must be a no-op against the
    # now-current, different bus.
    old_bus = _FakeBus("old")
    new_bus = _FakeBus("new")
    port = _OpenBusScriptedNetworkPort([])
    port._bus = cast(MessageBus, new_bus)
    port._interface = cast(ProxyInterface, _FakeInterface(healthy=True))

    await port._disconnect(cast(MessageBus, old_bus))  # a stale reference -- not the current bus

    assert port._bus is new_bus  # untouched
    assert new_bus.disconnect_calls == 0
    assert old_bus.disconnect_calls == 0  # the fake bus itself was never touched either


class _LeakyMessageBus:
    """Stands in for :class:`dbus_fast.aio.MessageBus`: ``connect()`` succeeds, ``introspect()`` fails.

    Exercises the real (unstubbed) :meth:`ComitupNetworkPort._open_bus`
    itself -- unlike ``_FakeBus``/``_OpenBusScriptedNetworkPort`` above,
    which stub ``_open_bus`` wholesale and so never run its body. Regression
    test for the leaked-fd bug: a bus that connects but never resolves the
    comitup interface must still be disconnected.
    """

    instances: ClassVar[list[_LeakyMessageBus]] = []

    def __init__(self, *, bus_type: Any = None) -> None:
        self.disconnect_calls = 0
        _LeakyMessageBus.instances.append(self)

    async def connect(self) -> _LeakyMessageBus:
        return self

    async def introspect(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("comitup is not on the bus")

    def disconnect(self) -> None:
        self.disconnect_calls += 1


async def test_open_bus_disconnects_a_connected_bus_when_introspect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _LeakyMessageBus.instances = []
    monkeypatch.setattr("palmimo_portal.adapters.comitup.MessageBus", _LeakyMessageBus)
    port = ComitupNetworkPort()

    with pytest.raises(ConnectionRefusedError):
        await port._connect()

    assert len(_LeakyMessageBus.instances) == 1
    assert _LeakyMessageBus.instances[0].disconnect_calls == 1


def test_get_status_maps_introspect_failure_to_adapter_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _LeakyMessageBus.instances = []
    monkeypatch.setattr("palmimo_portal.adapters.comitup.MessageBus", _LeakyMessageBus)
    port = ComitupNetworkPort()

    with pytest.raises(AdapterUnavailableError):
        port.get_status()

    # One connect attempt, one retry -- _call_resilient retries once -- each
    # opening (and each failing to disconnect-free) its own bus.
    assert len(_LeakyMessageBus.instances) == 2
    assert all(bus.disconnect_calls == 1 for bus in _LeakyMessageBus.instances)


class _HangingIntrospectMessageBus:
    """Stands in for :class:`dbus_fast.aio.MessageBus`: ``connect()`` succeeds, ``introspect()`` hangs forever.

    Exercises ``_connect()``'s outer ``asyncio.wait_for(..., timeout=CONNECT_TIMEOUT_SECONDS)``
    timing out and cancelling ``_open_bus()`` mid-flight -- distinct from
    ``_LeakyMessageBus`` above, which exercises ``introspect()`` failing
    outright. Regression test for the same leaked-fd bug reached a
    different way: a bus that connected but then never got a chance to
    finish resolving the comitup interface, because the caller gave up on
    it first.
    """

    instances: ClassVar[list[_HangingIntrospectMessageBus]] = []

    def __init__(self, *, bus_type: Any = None) -> None:
        self.disconnect_calls = 0
        _HangingIntrospectMessageBus.instances.append(self)

    async def connect(self) -> _HangingIntrospectMessageBus:
        return self

    async def introspect(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(10)  # far longer than CONNECT_TIMEOUT_SECONDS below

    def disconnect(self) -> None:
        self.disconnect_calls += 1


async def test_connect_disconnects_the_bus_when_open_bus_is_cancelled_by_its_own_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _HangingIntrospectMessageBus.instances = []
    monkeypatch.setattr("palmimo_portal.adapters.comitup.MessageBus", _HangingIntrospectMessageBus)
    monkeypatch.setattr("palmimo_portal.adapters.comitup.CONNECT_TIMEOUT_SECONDS", 0.01)
    port = ComitupNetworkPort()

    with pytest.raises(TimeoutError):
        await port._connect()

    assert len(_HangingIntrospectMessageBus.instances) == 1
    assert _HangingIntrospectMessageBus.instances[0].disconnect_calls == 1


async def test_a_timeout_is_never_retried() -> None:
    class _HangingInterface:
        def __init__(self) -> None:
            self.call_count = 0

        async def call_state(self) -> Any:
            self.call_count += 1
            await asyncio.sleep(10)  # far longer than the call's own timeout below

    port = _OpenBusScriptedNetworkPort([])
    port._bus = cast(MessageBus, _FakeBus("only"))
    interface = _HangingInterface()
    port._interface = cast(ProxyInterface, interface)

    with pytest.raises(AdapterUnavailableError):
        await port._call_resilient("state", (), 0.01)

    assert interface.call_count == 1  # no retry attempt
    assert port.open_bus_calls == 0  # no reconnect attempt either
