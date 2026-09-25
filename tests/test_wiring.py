"""The release channel applies to every GitHub release lookup, not only the Portal's own update."""

from pathlib import Path

import pytest

from palmimo_portal.adapters.catalog import GitHubCatalogSource
from palmimo_portal.adapters.github_releases import GitHubReleaseSource
from palmimo_portal.settings import Settings
from palmimo_portal.wiring import build_adapters


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
