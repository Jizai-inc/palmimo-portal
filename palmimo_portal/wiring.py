"""Builds the concrete adapter set :func:`~palmimo_portal.api.app.create_app` wires in.

Kept separate from :mod:`palmimo_portal.api.app` (the FastAPI wiring) so
nothing outside ``api/`` needs ``fastapi`` at all: this module imports only
the port protocols, the concrete adapters, the in-memory fakes, and
:class:`~palmimo_portal.settings.Settings` -- never ``fastapi``/``starlette``.
See ``tests/test_import_contracts.py`` for the import-discipline contract
this keeps.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import partial

from palmimo_portal.adapters.catalog import GitHubCatalogSource
from palmimo_portal.adapters.clock import SystemClockPort
from palmimo_portal.adapters.comitup import ComitupNetworkPort
from palmimo_portal.adapters.disk import OsDiskPort
from palmimo_portal.adapters.git_port import SubprocessGitPort
from palmimo_portal.adapters.git_uv_updater import GitUvUpdater
from palmimo_portal.adapters.github_releases import GitHubReleaseSource
from palmimo_portal.adapters.identity import FileIdentityStore
from palmimo_portal.adapters.journal import JournalctlPort
from palmimo_portal.adapters.networkmanager import NetworkManagerSavedNetworkPort
from palmimo_portal.adapters.platform import GitHubPlatformBundleSource, SudoPlatformPort
from palmimo_portal.adapters.rundir import TmpfsRunDirPort
from palmimo_portal.adapters.secrets import JsonSecretsStore
from palmimo_portal.adapters.ssh_keys import AuthorizedKeysSshKeyPort
from palmimo_portal.adapters.state import JsonFileStateStore, preflight_state_dir
from palmimo_portal.adapters.static_asset import repair_static_dir
from palmimo_portal.adapters.systemd import (
    SystemdAppUnitPort,
    SystemdComitupServicePort,
    SystemdSyncUnitPort,
    SystemdSystemPort,
    SystemdWifiQrUnitPort,
)
from palmimo_portal.adapters.uv_port import SubprocessUvPort
from palmimo_portal.adapters.wifi_qr_socket import WifiQrSocketClient
from palmimo_portal.core.wifi_attempt_manager import WifiAttemptManager, connect_legacy
from palmimo_portal.ports import (
    AppUnitPort,
    CatalogSource,
    ClockPort,
    ComitupServicePort,
    DiskPort,
    GitPort,
    IdentityStore,
    JournalPort,
    NetworkPort,
    PlatformBundleSource,
    PlatformPort,
    ReleaseSource,
    RunDirPort,
    SavedNetworkPort,
    SecretsStore,
    SshKeyPort,
    StateStore,
    SyncUnitPort,
    SystemPort,
    Updater,
    UvPort,
    WifiProfilePort,
    WifiQrClientFactory,
    WifiQrUnitPort,
)
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import (
    FakeAppUnitPort,
    FakeCatalogSource,
    FakeClockPort,
    FakeComitupServicePort,
    FakeDiskPort,
    FakeGitPort,
    FakeIdentityStore,
    FakeJournalPort,
    FakeNetworkPort,
    FakePlatformBundleSource,
    FakePlatformPort,
    FakeReleaseSource,
    FakeRunDirPort,
    FakeSavedNetworkPort,
    FakeSecretsStore,
    FakeSshKeyPort,
    FakeStateStore,
    FakeSyncUnitPort,
    FakeSystemPort,
    FakeUpdater,
    FakeUvPort,
    FakeWifiProfilePort,
    FakeWifiQrClientFactory,
    FakeWifiQrUnitPort,
)


# GitHub exposes one releases/latest per repository; devkit mixes SDK and examples releases.
CATALOG_RELEASE_TAG_PREFIX = "examples-v"


@dataclass(frozen=True)
class AdapterBundle:
    """The five ports, wired to concrete implementations."""

    network: NetworkPort
    saved_networks: SavedNetworkPort
    wifi_profiles: WifiProfilePort
    comitup_service: ComitupServicePort
    wifi_attempts: WifiAttemptManager
    wifi_qr_unit: WifiQrUnitPort
    wifi_qr_client: WifiQrClientFactory
    system: SystemPort
    ssh_keys: SshKeyPort
    state: StateStore
    identity: IdentityStore
    releases: ReleaseSource
    updater: Updater
    secrets: SecretsStore
    git: GitPort
    uv: UvPort
    sync_unit: SyncUnitPort
    disk: DiskPort
    app_unit: AppUnitPort
    run_dir: RunDirPort
    journal: JournalPort
    platform: PlatformPort
    platform_bundle: PlatformBundleSource
    platform_releases: ReleaseSource
    clock: ClockPort
    catalog: CatalogSource


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
    edit_lock = threading.RLock()
    if settings.adapters == "fake":
        fake_state = FakeStateStore()
        fake_profiles = FakeWifiProfilePort()
        fake_network = FakeNetworkPort(
            profiles=fake_profiles,
            known_profiles=lambda: fake_attempts.has_known_networks(),
            display_ssid=lambda ssid: fake_attempts.display_ssid(ssid),
        )
        fake_service = FakeComitupServicePort(network=fake_network)
        fake_saved = FakeSavedNetworkPort(profiles=fake_profiles)
        fake_attempts = WifiAttemptManager(
            fake_profiles,
            fake_service,
            fake_state,
            edit_lock=edit_lock,
            legacy_connect=partial(connect_legacy, fake_network, fake_saved),
            legacy_status=fake_network.get_status,
        )
        return AdapterBundle(
            network=fake_network,
            saved_networks=fake_saved,
            wifi_profiles=fake_profiles,
            comitup_service=fake_service,
            wifi_attempts=fake_attempts,
            wifi_qr_unit=FakeWifiQrUnitPort(),
            wifi_qr_client=FakeWifiQrClientFactory(),
            system=FakeSystemPort(),
            ssh_keys=FakeSshKeyPort(),
            state=fake_state,
            identity=FakeIdentityStore(),
            releases=FakeReleaseSource(),
            updater=FakeUpdater(),
            secrets=FakeSecretsStore(),
            git=FakeGitPort(),
            uv=FakeUvPort(),
            sync_unit=FakeSyncUnitPort(),
            disk=FakeDiskPort(),
            app_unit=FakeAppUnitPort(),
            run_dir=FakeRunDirPort(),
            journal=FakeJournalPort(),
            platform=FakePlatformPort(),
            platform_bundle=FakePlatformBundleSource(),
            platform_releases=FakeReleaseSource(),
            clock=FakeClockPort(),
            catalog=FakeCatalogSource(),
        )
    preflight_state_dir(settings.state_dir)
    real_state = JsonFileStateStore(settings.state_dir)
    saved_networks = NetworkManagerSavedNetworkPort(edit_lock=edit_lock, wlan_interface=settings.wlan_interface)
    comitup_service = SystemdComitupServicePort()
    network = ComitupNetworkPort(
        known_profiles=lambda: attempts.has_known_networks(),
        display_ssid=lambda ssid: attempts.display_ssid(ssid),
        wlan_interface=settings.wlan_interface,
    )
    attempts = WifiAttemptManager(
        saved_networks,
        comitup_service,
        real_state,
        edit_lock=edit_lock,
        legacy_connect=partial(connect_legacy, network, saved_networks),
        legacy_status=network.get_status,
    )
    return AdapterBundle(
        network=network,
        saved_networks=saved_networks,
        wifi_profiles=saved_networks,
        comitup_service=comitup_service,
        wifi_attempts=attempts,
        wifi_qr_unit=SystemdWifiQrUnitPort(),
        wifi_qr_client=lambda message, disconnected: WifiQrSocketClient(message, on_disconnect=disconnected),
        system=SystemdSystemPort(unit=settings.portal_unit),
        ssh_keys=AuthorizedKeysSshKeyPort(),
        state=real_state,
        identity=FileIdentityStore(settings.identity_file),
        releases=GitHubReleaseSource(repo=settings.update_repo, channel=settings.update_channel),
        updater=GitUvUpdater(portal_dir=settings.portal_dir, uv_bin=settings.uv_bin, update_repo=settings.update_repo),
        secrets=JsonSecretsStore(settings.secrets_dir),
        git=SubprocessGitPort(),
        uv=SubprocessUvPort(uv_bin=settings.uv_bin),
        sync_unit=SystemdSyncUnitPort(),
        disk=OsDiskPort(),
        app_unit=SystemdAppUnitPort(),
        run_dir=TmpfsRunDirPort(settings.run_dir),
        journal=JournalctlPort(),
        platform=SudoPlatformPort(installed_path=settings.platform_dir / "installed.json", sudo_bin=settings.sudo_bin),
        platform_bundle=GitHubPlatformBundleSource(platform_repo=settings.platform_repo),
        platform_releases=GitHubReleaseSource(repo=settings.platform_repo, channel=settings.update_channel),
        clock=SystemClockPort(),
        catalog=GitHubCatalogSource(
            catalog_repo=settings.catalog_repo,
            release_source=GitHubReleaseSource(
                repo=settings.catalog_repo,
                channel=settings.update_channel,
                tag_prefix=CATALOG_RELEASE_TAG_PREFIX,
            ),
        ),
    )
