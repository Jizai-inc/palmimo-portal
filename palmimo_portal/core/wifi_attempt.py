"""Pure resolution rule for a pending Wi-Fi connect attempt.

``POST /wifi/connect`` (:mod:`palmimo_portal.api.wifi`) writes a
:class:`~palmimo_portal.ports.WifiAttempt` record as ``"attempting"`` and
returns immediately -- comitup's own connection attempt happens
asynchronously, with no way to observe its outcome on that response (the
AP-disconnection asymmetry: connecting to the home network tears down the
setup AP the client was talking through). The only way to learn the
outcome is to keep polling :class:`~palmimo_portal.ports.NetworkPort.get_status`
and watch for the state to settle.

:func:`resolve_attempt` is that decision, extracted as a pure function so
both :class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort` and
:class:`~palmimo_portal.testing.fakes.FakeNetworkPort` consume the same
logic rather than two hand-maintained copies that can drift apart.

Three cases this rule exists to get right:

- **A genuinely observed transition** (``CONNECTING`` -> ``CONNECTED``, or
  ``CONNECTING`` -> ``HOTSPOT``): the common case.
- **The first observation after a process restart, or a round trip the
  poller never saw** (e.g. comitup settles, moves on, and resettles
  between two polls that both land on the same state). Either way there
  is no "previous state" to detect a change against, so the caller must
  invoke this rule on *every* observation, not only on a detected change.
- **The reconfigure race: "connected" must mean the requested network.**
  ``POST /wifi/connect`` while already ``CONNECTED`` forgets the old
  network before calling comitup's own ``connect()``, but that
  forget-then-connect sequence is not instantaneous -- for a short window
  a status poll can still observe ``CONNECTED`` to the *old* SSID. So
  when the observed state is ``CONNECTED`` but the name is neither
  ``None`` nor the attempt's own ``ssid``: within
  :data:`GRACE_PERIOD_SECONDS`, this resolves to nothing yet; past the
  grace period, it resolves to ``"failed"`` with the observed name. A
  name that is ``None`` or matches ``ssid`` resolves to ``"connected"``
  immediately.
"""

from __future__ import annotations

from dataclasses import dataclass

from palmimo_portal.ports import ConnectionState, WifiAttempt


ATTEMPT_RESULT_ATTEMPTING = "attempting"
ATTEMPT_RESULT_CONNECTED = "connected"
ATTEMPT_RESULT_FAILED = "failed"

#: How long a fresh "attempting" record is protected from being resolved
#: away from an observation that has not caught up with it yet. Guards a
#: ``HOTSPOT`` observation racing a poll that has not yet seen comitup
#: leave ``HOTSPOT``, and a ``CONNECTED`` observation whose name does not
#: match the attempt's own ``ssid`` -- the reconfigure race (see the
#: module docstring). A matching (or unknown) name is never subject to
#: this grace period.
GRACE_PERIOD_SECONDS = 10.0

_SETTLED_STATES = frozenset({ConnectionState.CONNECTED, ConnectionState.UNPROVISIONED})


def is_settled(state: ConnectionState) -> bool:
    """Report whether *state* is a settled outcome (``CONNECTED`` or ``HOTSPOT``/``UNPROVISIONED``).

    ``CONNECTING`` is deliberately not settled -- it is comitup's own
    "still trying" state, and never resolves a pending attempt.
    """
    return state in _SETTLED_STATES


@dataclass(frozen=True)
class AttemptResolution:
    """What :func:`resolve_attempt` decided: how to resolve the pending attempt, and why."""

    result: str
    """``"connected"`` or ``"failed"``."""

    observed_connection_name: str | None
    """The connection/AP name observed alongside the settled state -- kept
    distinct from the attempt's own ``ssid`` (see :class:`~palmimo_portal.ports.WifiAttempt`)
    since comitup can settle onto a network other than the one just
    attempted."""

    reason: str
    """``"first_observation"`` or ``"transition"`` -- for the caller's log line."""


def resolve_attempt(
    *,
    attempt: WifiAttempt | None,
    observed_state: ConnectionState,
    is_first_observation: bool,
    observed_connection_name: str | None,
    now: float,
) -> AttemptResolution | None:
    """Decide whether/how to resolve a pending ``"attempting"`` record, given one fresh observation.

    Returns ``None`` when: no pending attempt, already resolved, observed
    state is ``CONNECTING`` (never settled), or observed state is
    ``HOTSPOT``/``CONNECTED``-to-a-different-network within the grace
    period (see :data:`GRACE_PERIOD_SECONDS` and the module docstring's
    reconfigure race).

    Otherwise returns the :class:`AttemptResolution` to record:
    ``"connected"`` for ``CONNECTED`` whose name is ``None`` or matches
    the attempt's ``ssid``; ``"failed"`` for ``HOTSPOT`` past the grace
    period (always ``observed_connection_name=None`` -- comitup's hotspot
    broadcast name is not a network Palmimo joined), or for ``CONNECTED``
    to a different network past the grace period (that name is kept).
    Applies identically regardless of *is_first_observation*.
    """
    if attempt is None or attempt.result != ATTEMPT_RESULT_ATTEMPTING:
        return None
    if not is_settled(observed_state):
        return None

    reason = "first_observation" if is_first_observation else "transition"
    elapsed = now - attempt.timestamp
    # A negative elapsed time means the wall clock stepped backwards (e.g. a
    # power-cut reboot on a Pi with no RTC) -- treat that as an expired
    # grace period rather than a real duration.
    within_grace_period = 0 <= elapsed < GRACE_PERIOD_SECONDS

    if observed_state is ConnectionState.CONNECTED:
        settled_on_a_different_network = (
            observed_connection_name is not None and observed_connection_name != attempt.ssid
        )
        if settled_on_a_different_network and within_grace_period:
            return None  # the reconfigure race: the old network, still winding down
        if settled_on_a_different_network:
            return AttemptResolution(
                result=ATTEMPT_RESULT_FAILED, observed_connection_name=observed_connection_name, reason=reason
            )
        return AttemptResolution(
            result=ATTEMPT_RESULT_CONNECTED, observed_connection_name=observed_connection_name, reason=reason
        )

    # observed_state is ConnectionState.UNPROVISIONED (comitup's HOTSPOT).
    if within_grace_period:
        return None
    # comitup reports its AP's own broadcast name (e.g. "palmimo-XXXX") as
    # the connection name even while in HOTSPOT -- that is not a network
    # Palmimo "joined", so force observed_connection_name to None here
    # rather than relying on every caller to already pass None.
    return AttemptResolution(result=ATTEMPT_RESULT_FAILED, observed_connection_name=None, reason=reason)
