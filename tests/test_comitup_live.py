"""Live tests: talk to a real comitup D-Bus service on the system bus.

These only make sense on a device actually running comitup (a Pi, or a dev
box with comitup installed and comitup.service active) -- CI never runs
them. Every test here is marked ``live`` and skipped by default; pass
``--live`` (see ``conftest.py``) to run this file, e.g.::

    uv run pytest tests/test_comitup_live.py --live -v

Deliberately does NOT cover ``connect()`` or a reboot/shutdown equivalent --
both are disruptive on a real device (a connect attempt can tear down the
very Wi-Fi link the test is running over; a reboot needs no explanation).
Only read-only calls are exercised here.
"""

from __future__ import annotations

import pytest

from palmimo_portal.adapters.comitup import ComitupNetworkPort
from palmimo_portal.ports import ConnectionState


pytestmark = pytest.mark.live


@pytest.fixture
def network() -> ComitupNetworkPort:
    return ComitupNetworkPort()


def test_state_returns_a_known_connection_state(network: ComitupNetworkPort) -> None:
    status = network.get_status()

    assert status.state in (ConnectionState.UNPROVISIONED, ConnectionState.CONNECTING, ConnectionState.CONNECTED)


def test_access_points_returns_a_non_empty_scan(network: ComitupNetworkPort) -> None:
    # access_points() triggers a live radio scan -- there should be at
    # least one nearby network in any real environment this runs in
    # (including, at minimum, this device's own hotspot/neighbors).
    networks = network.list_networks()

    assert len(networks) > 0


def test_get_info_has_an_apname(network: ComitupNetworkPort) -> None:
    info = network._loop_thread.run(
        network._call_resilient("get_info", (), timeout=5.0),
        timeout=6.0,
    )

    assert info.get("apname", "") != ""
