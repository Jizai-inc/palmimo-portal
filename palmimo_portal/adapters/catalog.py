"""Real :class:`~palmimo_portal.ports.CatalogSource`: the devkit release's ``palmimo-catalog-<tag>.json`` asset.

Shares :mod:`palmimo_portal.adapters.static_asset`'s ``download``/
``verify_checksum`` primitives with the frontend-build and platform-bundle
fetchers -- the catalog asset is a single JSON file plus a ``.sha256``
sidecar, no archive to extract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from palmimo_portal.adapters.static_asset import (
    Opener,
    StaticAssetError,
    asset_url,
    default_opener,
    download,
    verify_checksum,
)
from palmimo_portal.core.manifest import (
    DEFAULT_MANIFEST_FILENAME,
    InvalidManifestFilenameError,
    _is_http_url,
    validate_manifest_filename,
)
from palmimo_portal.ports import (
    AppSource,
    CatalogApp,
    CatalogAsset,
    CatalogEnvSpec,
    CatalogSource,
    CatalogSourceError,
    Release,
    ReleaseSource,
    ReleaseSourceError,
)
from palmimo_portal.version import portal_version


_REQUIRED_APP_KEYS = ("name", "description", "source", "env", "devices")
_REQUIRED_SOURCE_KEYS = ("type", "url", "ref", "ref_kind", "commit")
_REQUIRED_ENV_KEYS = ("required", "description")


def _parse_source(raw: Any, asset_name: str, index: int) -> AppSource:
    if not isinstance(raw, dict) or any(key not in raw for key in _REQUIRED_SOURCE_KEYS):
        raise CatalogSourceError(f"{asset_name}.apps[{index}].source is missing one of {_REQUIRED_SOURCE_KEYS}")
    try:
        manifest = validate_manifest_filename(raw.get("manifest"))
    except InvalidManifestFilenameError as error:
        raise CatalogSourceError(f"{asset_name}.apps[{index}].source.manifest is invalid: {error}") from error
    return AppSource(
        type=raw["type"],
        url=raw["url"],
        ref=raw["ref"],
        ref_kind=raw["ref_kind"],
        subdir=raw.get("subdir"),
        commit=raw["commit"],
        manifest=None if manifest == DEFAULT_MANIFEST_FILENAME else manifest,
    )


def _parse_env(raw: Any, asset_name: str, index: int) -> dict[str, CatalogEnvSpec]:
    if not isinstance(raw, dict):
        raise CatalogSourceError(f"{asset_name}.apps[{index}].env must be an object")
    env: dict[str, CatalogEnvSpec] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or any(key not in spec for key in _REQUIRED_ENV_KEYS):
            raise CatalogSourceError(f"{asset_name}.apps[{index}].env.{name} is missing one of {_REQUIRED_ENV_KEYS}")
        # A malformed help_url drops just that field rather than the whole catalog -- unlike the
        # required keys above, it is decoration the app-detail UI can live without.
        help_url = spec.get("help_url")
        env[name] = CatalogEnvSpec(
            required=bool(spec["required"]),
            description=str(spec["description"]),
            help_url=help_url if _is_http_url(help_url) else None,
        )
    return env


def _parse_catalog(asset_bytes: bytes, asset_name: str) -> tuple[CatalogApp, ...]:
    try:
        data: Any = json.loads(asset_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogSourceError(f"{asset_name} is not valid JSON: {error}") from error
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise CatalogSourceError(f"{asset_name} must be an object with schema == 1")
    raw_apps = data.get("apps")
    if not isinstance(raw_apps, list):
        raise CatalogSourceError(f"{asset_name}.apps must be a list")
    apps: list[CatalogApp] = []
    for index, entry in enumerate(raw_apps):
        if not isinstance(entry, dict) or any(key not in entry for key in _REQUIRED_APP_KEYS):
            raise CatalogSourceError(f"{asset_name}.apps[{index}] is missing one of {_REQUIRED_APP_KEYS}")
        apps.append(
            CatalogApp(
                name=str(entry["name"]),
                description=str(entry["description"]),
                source=_parse_source(entry["source"], asset_name, index),
                env=_parse_env(entry["env"], asset_name, index),
                devices=tuple(entry["devices"]),
            )
        )
    return tuple(apps)


@dataclass
class GitHubCatalogSource(CatalogSource):
    """Fetches the selected ``catalog_repo`` release's catalog asset (design doc 4.1)."""

    catalog_repo: str
    release_source: ReleaseSource
    opener: Opener = field(default=default_opener)

    def fetch(self) -> CatalogAsset:
        try:
            release: Release = self.release_source.fetch_latest()
        except ReleaseSourceError as error:
            raise CatalogSourceError(str(error), code=error.code) from error

        asset_name = f"palmimo-catalog-{release.tag}.json"
        base_url = asset_url(self.catalog_repo, release.tag, asset_name)
        user_agent = f"palmimo-portal/{portal_version()}"
        try:
            asset_bytes = download(self.opener, base_url, user_agent)
            sha_bytes = download(self.opener, f"{base_url}.sha256", user_agent)
            verify_checksum(asset_bytes, sha_bytes, asset_name)
        except StaticAssetError as error:
            raise CatalogSourceError(str(error)) from error

        apps = _parse_catalog(asset_bytes, asset_name)
        return CatalogAsset(tag=release.tag, apps=apps)
