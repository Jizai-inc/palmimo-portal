"""The unprovisioned-state rule and which endpoints it gates.

Unprovisioned means "no Wi-Fi connection and no saved infrastructure profile"
— the out-of-box state before a buyer picks their home network. While
unprovisioned, only the Wi-Fi endpoints, first-time password setup, and
system status are served; every other endpoint answers 409
``not_provisioned``. Server-side gate: the setup AP is open to anyone on
it, so the backend must refuse on its own.
"""

from __future__ import annotations

from palmimo_portal.ports import ConnectionState, NetworkPort


class NotProvisionedError(Exception):
    """Raised when a gated endpoint is called while the device is unprovisioned."""


def is_provisioned(network: NetworkPort) -> bool:
    """Report whether the device has left the out-of-box Wi-Fi state.

    Disconnected devices require a successful profile read before setup can reopen.
    """
    status = network.get_status()
    return status.state is not ConnectionState.UNPROVISIONED or network.has_known_networks()


def is_serving_setup_ap(network: NetworkPort) -> bool:
    """Report whether the device is serving its setup AP right now.

    Unlike :func:`is_provisioned`, a provisioned device that fell back to the
    AP (its known networks out of range) counts too: ``UNPROVISIONED`` is
    comitup's ``HOTSPOT`` state, not the out-of-box rule.
    """
    return network.get_status().state is ConnectionState.UNPROVISIONED


def require_provisioned(network: NetworkPort) -> None:
    """Raise :class:`NotProvisionedError` unless the device is provisioned.

    Call from gated endpoints: SSH keys, system reboot/shutdown, auth
    login/logout. Wi-Fi endpoints, auth setup, and system status must
    never call this — they stay reachable while unprovisioned.
    """
    if not is_provisioned(network):
        raise NotProvisionedError()
