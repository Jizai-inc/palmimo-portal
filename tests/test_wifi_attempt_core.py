"""Table tests for the pure :func:`~palmimo_portal.core.wifi_attempt.resolve_attempt` rule."""

from __future__ import annotations

import pytest

from palmimo_portal.core.wifi_attempt import GRACE_PERIOD_SECONDS, AttemptResolution, is_settled, resolve_attempt
from palmimo_portal.ports import ConnectionState, WifiAttempt


ATTEMPTING = WifiAttempt(ssid="home", result="attempting", timestamp=1000.0)
ALREADY_CONNECTED = WifiAttempt(ssid="home", result="connected", timestamp=1000.0)
ALREADY_FAILED = WifiAttempt(ssid="home", result="failed", timestamp=1000.0)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ConnectionState.CONNECTED, True),
        (ConnectionState.UNPROVISIONED, True),
        (ConnectionState.CONNECTING, False),
    ],
)
def test_is_settled(state: ConnectionState, expected: bool) -> None:
    assert is_settled(state) is expected


def test_no_pending_attempt_resolves_to_nothing() -> None:
    result = resolve_attempt(
        attempt=None,
        observed_state=ConnectionState.CONNECTED,
        is_first_observation=True,
        observed_connection_name="home",
        now=1000.0,
    )

    assert result is None


@pytest.mark.parametrize("attempt", [ALREADY_CONNECTED, ALREADY_FAILED])
def test_an_already_resolved_attempt_is_left_alone(attempt: WifiAttempt) -> None:
    result = resolve_attempt(
        attempt=attempt,
        observed_state=ConnectionState.CONNECTED,
        is_first_observation=True,
        observed_connection_name="home",
        now=1000.0,
    )

    assert result is None


@pytest.mark.parametrize("is_first_observation", [True, False])
def test_connecting_never_resolves(is_first_observation: bool) -> None:
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.CONNECTING,
        is_first_observation=is_first_observation,
        observed_connection_name=None,
        now=1000.0,
    )

    assert result is None


@pytest.mark.parametrize("is_first_observation", [True, False])
def test_connected_resolves_immediately_regardless_of_grace_period(is_first_observation: bool) -> None:
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.CONNECTED,
        is_first_observation=is_first_observation,
        observed_connection_name="home",
        now=ATTEMPTING.timestamp + 0.001,  # essentially no time elapsed
    )

    assert result == AttemptResolution(
        result="connected",
        observed_connection_name="home",
        reason="first_observation" if is_first_observation else "transition",
    )


def test_connected_to_a_different_network_within_the_grace_period_does_not_resolve_yet() -> None:
    # The reconfigure race: right after the request, the *old* network can
    # still observably be CONNECTED for a moment before comitup's
    # forget-then-connect sequence actually starts. Must not be read as
    # "connected" (to the wrong network).
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.CONNECTED,
        is_first_observation=False,
        observed_connection_name="some-other-known-network",
        now=ATTEMPTING.timestamp + 1,
    )

    assert result is None


@pytest.mark.parametrize("is_first_observation", [True, False])
def test_connected_to_a_different_network_past_the_grace_period_resolves_failed(is_first_observation: bool) -> None:
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.CONNECTED,
        is_first_observation=is_first_observation,
        observed_connection_name="some-other-known-network",
        now=ATTEMPTING.timestamp + GRACE_PERIOD_SECONDS + 0.01,
    )

    assert result == AttemptResolution(
        result="failed",
        observed_connection_name="some-other-known-network",
        reason="first_observation" if is_first_observation else "transition",
    )


def test_connected_to_a_different_network_exactly_at_the_grace_period_boundary_resolves() -> None:
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.CONNECTED,
        is_first_observation=False,
        observed_connection_name="some-other-known-network",
        now=ATTEMPTING.timestamp + GRACE_PERIOD_SECONDS,
    )

    assert result is not None
    assert result.result == "failed"


def test_connected_with_no_observed_name_resolves_connected_regardless_of_grace_period() -> None:
    # observed_connection_name=None means the adapter reported no name --
    # there is nothing to compare against attempt.ssid, so this is not a
    # "different network" case at all (mirrors the CONNECTED-immediately
    # case in test_connected_resolves_immediately_regardless_of_grace_period).
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.CONNECTED,
        is_first_observation=False,
        observed_connection_name=None,
        now=ATTEMPTING.timestamp + 1,
    )

    assert result == AttemptResolution(result="connected", observed_connection_name=None, reason="transition")


def test_hotspot_within_the_grace_period_does_not_resolve() -> None:
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.UNPROVISIONED,
        is_first_observation=False,
        observed_connection_name=None,
        now=ATTEMPTING.timestamp + GRACE_PERIOD_SECONDS - 0.01,
    )

    assert result is None


@pytest.mark.parametrize("is_first_observation", [True, False])
def test_hotspot_past_the_grace_period_resolves_to_failed(is_first_observation: bool) -> None:
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.UNPROVISIONED,
        is_first_observation=is_first_observation,
        observed_connection_name=None,
        now=ATTEMPTING.timestamp + GRACE_PERIOD_SECONDS + 0.01,
    )

    assert result == AttemptResolution(
        result="failed",
        observed_connection_name=None,
        reason="first_observation" if is_first_observation else "transition",
    )


@pytest.mark.parametrize("is_first_observation", [True, False])
def test_hotspot_past_the_grace_period_discards_the_aps_own_broadcast_name(is_first_observation: bool) -> None:
    # comitup's `state()` call reports its own hotspot's broadcast SSID
    # (e.g. "jizaiten-ap") as the connection name even while in HOTSPOT --
    # see adapters/comitup.py's module docstring. That is not a network
    # Palmimo "joined", so it must never be recorded as
    # observed_connection_name, even though the caller passed a non-None
    # name through.
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.UNPROVISIONED,
        is_first_observation=is_first_observation,
        observed_connection_name="jizaiten-ap",
        now=ATTEMPTING.timestamp + GRACE_PERIOD_SECONDS + 0.01,
    )

    assert result == AttemptResolution(
        result="failed",
        observed_connection_name=None,
        reason="first_observation" if is_first_observation else "transition",
    )


def test_hotspot_exactly_at_the_grace_period_boundary_resolves() -> None:
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=ConnectionState.UNPROVISIONED,
        is_first_observation=False,
        observed_connection_name=None,
        now=ATTEMPTING.timestamp + GRACE_PERIOD_SECONDS,
    )

    assert result is not None
    assert result.result == "failed"


@pytest.mark.parametrize(
    ("observed_state", "observed_connection_name"),
    [
        (ConnectionState.UNPROVISIONED, None),
        (ConnectionState.CONNECTED, "some-other-known-network"),
    ],
)
@pytest.mark.parametrize("is_first_observation", [True, False])
def test_a_wall_clock_that_stepped_backwards_treats_the_grace_period_as_expired(
    observed_state: ConnectionState, observed_connection_name: str | None, is_first_observation: bool
) -> None:
    # Power-cut reboot on a Pi with no RTC: `now` can land before the
    # attempt's own timestamp, making `elapsed` negative. A negative elapsed
    # time is not "within" any real duration -- it must resolve exactly like
    # an expired grace period, not fall through the naive `elapsed <
    # GRACE_PERIOD_SECONDS` comparison and be treated as still fresh.
    result = resolve_attempt(
        attempt=ATTEMPTING,
        observed_state=observed_state,
        is_first_observation=is_first_observation,
        observed_connection_name=observed_connection_name,
        now=ATTEMPTING.timestamp - 5.0,
    )

    assert result == AttemptResolution(
        result="failed",
        observed_connection_name=observed_connection_name,
        reason="first_observation" if is_first_observation else "transition",
    )
