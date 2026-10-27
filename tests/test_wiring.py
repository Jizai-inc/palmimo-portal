"""The release channel applies to every GitHub release lookup, not only the Portal's own update."""

import time
from pathlib import Path

import pytest

from palmimo_portal.adapters.catalog import GitHubCatalogSource
from palmimo_portal.adapters.github_releases import GitHubReleaseSource
from palmimo_portal.ports import ConnectionState
from palmimo_portal.settings import Settings
from palmimo_portal.wiring import build_adapters


@pytest.mark.parametrize("saved", [False, True])
def test_default_fake_wifi_attempt_completes_and_updates_network_status(tmp_path: Path, saved: bool) -> None:
    bundle = build_adapters(Settings(adapters="fake", static_dir=tmp_path / "static"))
    if saved:
        bundle.saved_networks.save("home", "password", first=True)
        attempt_id = bundle.wifi_attempts.activate_saved("home")
    else:
        attempt_id = bundle.wifi_attempts.start_wifi_attempt("credentials", b"home", "password", None)

    deadline = time.monotonic() + 5
    while bundle.wifi_attempts.in_progress and time.monotonic() < deadline:
        time.sleep(0.01)

    assert bundle.wifi_attempts.outcome(attempt_id) == "connected"
    status = bundle.network.get_status()
    assert status.state is ConnectionState.CONNECTED
    assert status.ssid == "home"
    assert status.ip_address is not None
    assert [(p.ssid, p.active) for p in bundle.saved_networks.list_saved()] == [("home", True)]
    attempt = bundle.state.read_last_wifi_attempt()
    assert attempt is not None and attempt.result == "connected"


@pytest.mark.parametrize("channel", ["stable", "prerelease"])
def test_platform_and_catalog_release_lookups_follow_the_update_channel(tmp_path: Path, channel: str) -> None:
    settings = Settings(adapters="real", state_dir=tmp_path / "state", update_channel=channel)  # type: ignore[arg-type]
    (tmp_path / "state").mkdir()

    bundle = build_adapters(settings)

    assert isinstance(bundle.platform_releases, GitHubReleaseSource)
    assert bundle.platform_releases.channel == channel
    assert isinstance(bundle.catalog, GitHubCatalogSource)
    assert isinstance(bundle.catalog.release_source, GitHubReleaseSource)
    assert bundle.catalog.release_source.channel == channel
