"""The unprovisioned-state rule and which endpoints it gates.

Unprovisioned means "no Wi-Fi connection and none has ever been configured"
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

    True once connected or connecting (mid-attempt), or once any network
    has ever been configured — even if currently out of range of it. Only
    "never connected and nothing configured" is unprovisioned.
    """
    status = network.get_status()
    if status.state is not ConnectionState.UNPROVISIONED:
        return True
    return network.has_known_networks()


def require_provisioned(network: NetworkPort) -> None:
    """Raise :class:`NotProvisionedError` unless the device is provisioned.

    Call from gated endpoints: SSH keys, system reboot/shutdown, auth
    login/logout. Wi-Fi endpoints, auth setup, and system status must
    never call this — they stay reachable while unprovisioned.
    """
    if not is_provisioned(network):
        raise NotProvisionedError()
