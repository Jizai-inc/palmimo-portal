"""Pure resolution rule for a pending Wi-Fi connect attempt.

``POST /wifi/connect`` writes a :class:`~palmimo_portal.ports.WifiAttempt`
record as ``"attempting"`` and returns immediately -- comitup's own
connection attempt happens asynchronously (the AP-disconnection asymmetry:
connecting to the home network tears down the setup AP the client was
talking through), so the outcome can only be learned by polling
:class:`~palmimo_portal.ports.NetworkPort.get_status` until it settles.
:func:`resolve_attempt` is that decision, a pure function shared by
:class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort` and
:class:`~palmimo_portal.testing.fakes.FakeNetworkPort` so the logic cannot drift apart.

Must be invoked on *every* observation, not only on a detected state
change -- there is no reliable "previous state" (first observation after a
restart, or a round trip the poller never saw). The reconfigure race:
``POST /wifi/connect`` while already ``CONNECTED`` forgets the old network
before calling comitup's own ``connect()``, but that sequence is not
instantaneous, so a status poll can briefly still observe ``CONNECTED`` to
the *old* SSID. When the observed state is ``CONNECTED`` with a name that
is neither ``None`` nor the attempt's own ``ssid``: within
:data:`GRACE_PERIOD_SECONDS` this resolves to nothing yet; past it,
``"failed"`` with the observed name. A name that is ``None`` or matches
``ssid`` resolves to ``"connected"`` immediately.
"""

from __future__ import annotations

from dataclasses import dataclass

from palmimo_portal.ports import ConnectionState, WifiAttempt


ATTEMPT_RESULT_ATTEMPTING = "attempting"
ATTEMPT_RESULT_CONNECTED = "connected"
ATTEMPT_RESULT_FAILED = "failed"

#: How long a fresh "attempting" record is protected from being resolved
#: away from a stale observation -- guards a ``HOTSPOT`` poll racing
#: comitup leaving ``HOTSPOT``, and the reconfigure race (module docstring).
#: A matching (or unknown) name is never subject to this grace period.
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
    """Kept distinct from the attempt's own ``ssid``: comitup can settle onto a different network."""

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
    ``HOTSPOT``/``CONNECTED``-to-a-different-network within
    :data:`GRACE_PERIOD_SECONDS` (module docstring's reconfigure race).
    Otherwise returns the :class:`AttemptResolution`: ``"connected"`` for
    ``CONNECTED`` whose name is ``None`` or matches ``ssid``; ``"failed"``
    for ``HOTSPOT`` past the grace period (``observed_connection_name=None``
    -- comitup's hotspot broadcast name is not a network Palmimo joined),
    or for ``CONNECTED`` to a different network past the grace period
    (that name is kept).
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
