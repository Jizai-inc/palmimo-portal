"""Builds the concrete adapter set :func:`~palmimo_portal.api.app.create_app` wires in.

Kept separate from :mod:`palmimo_portal.api.app` (the FastAPI wiring) so
nothing outside ``api/`` needs ``fastapi`` at all: this module imports only
the port protocols, the concrete adapters, the in-memory fakes, and
:class:`~palmimo_portal.settings.Settings` -- never ``fastapi``/``starlette``.
See ``tests/test_import_contracts.py`` for the import-discipline contract
this keeps.
"""

from __future__ import annotations

from dataclasses import dataclass

from palmimo_portal.adapters.comitup import ComitupNetworkPort
from palmimo_portal.adapters.git_uv_updater import GitUvUpdater
from palmimo_portal.adapters.github_releases import GitHubReleaseSource
from palmimo_portal.adapters.identity import FileIdentityStore
from palmimo_portal.adapters.ssh_keys import AuthorizedKeysSshKeyPort
from palmimo_portal.adapters.state import JsonFileStateStore, preflight_state_dir
from palmimo_portal.adapters.static_asset import repair_static_dir
from palmimo_portal.adapters.systemd import SystemdSystemPort
from palmimo_portal.ports import IdentityStore, NetworkPort, ReleaseSource, SshKeyPort, StateStore, SystemPort, Updater
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import (
    FakeIdentityStore,
    FakeNetworkPort,
    FakeReleaseSource,
    FakeSshKeyPort,
    FakeStateStore,
    FakeSystemPort,
    FakeUpdater,
)


# The real NetworkPort's known-network marker lives alongside auth.json and
# the rest of this process's own persisted state -- see
# palmimo_portal.adapters.comitup's module docstring for why it exists.
KNOWN_NETWORK_MARKER_FILENAME = "network_known.marker"


@dataclass(frozen=True)
class AdapterBundle:
    """The five ports, wired to concrete implementations."""

    network: NetworkPort
    system: SystemPort
    ssh_keys: SshKeyPort
    state: StateStore
    identity: IdentityStore
    releases: ReleaseSource
    updater: Updater


def build_adapters(settings: Settings) -> AdapterBundle:
    """Build the adapter bundle :func:`~palmimo_portal.api.app.create_app` wires into the app.

    ``settings.adapters == "fake"`` (default) wires every port to an in-memory fake; ``"real"``
    wires every port to its OS-backed adapter (comitup/logind over D-Bus, filesystem for the rest).

    When ``"real"``, runs :func:`~palmimo_portal.adapters.state.preflight_state_dir` first -- an
    unwritable or root-owned state directory fails loudly here, before uvicorn ever binds,
    instead of surfacing as an opaque 500 on ``/setup``. Also runs
    :func:`~palmimo_portal.adapters.static_asset.repair_static_dir` unconditionally, before
    ``_mount_frontend`` looks at ``settings.static_dir`` -- pure filesystem repair (undoing a
    power loss mid-swap) regardless of ``settings.adapters``.
    """
    repair_static_dir(settings.static_dir)
    if settings.adapters == "fake":
        fake_state = FakeStateStore()
        return AdapterBundle(
            network=FakeNetworkPort(state_store=fake_state),
            system=FakeSystemPort(),
            ssh_keys=FakeSshKeyPort(),
            state=fake_state,
            identity=FakeIdentityStore(),
            releases=FakeReleaseSource(),
            updater=FakeUpdater(),
        )
    preflight_state_dir(settings.state_dir)
    real_state = JsonFileStateStore(settings.state_dir)
    return AdapterBundle(
        network=ComitupNetworkPort(
            known_network_marker=settings.state_dir / KNOWN_NETWORK_MARKER_FILENAME,
            state_store=real_state,
        ),
        system=SystemdSystemPort(unit=settings.portal_unit),
        ssh_keys=AuthorizedKeysSshKeyPort(),
        state=real_state,
        identity=FileIdentityStore(settings.identity_file),
        releases=GitHubReleaseSource(repo=settings.update_repo),
        updater=GitUvUpdater(portal_dir=settings.portal_dir, uv_bin=settings.uv_bin, update_repo=settings.update_repo),
    )
