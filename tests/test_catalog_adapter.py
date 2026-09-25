"""Tests for :mod:`palmimo_portal.adapters.catalog`. No network access -- an injected opener stands in."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any

import pytest

from palmimo_portal.adapters.catalog import GitHubCatalogSource
from palmimo_portal.ports import AppSource, CatalogApp, CatalogEnvSpec, CatalogSourceError, Release, ReleaseSource


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, _max_bytes: int) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _FakeReleaseSource(ReleaseSource):
    def fetch_latest(self) -> Release:
        return Release(tag="v1.0.0", name="v1.0.0", published_at="2026-01-01T00:00:00Z", html_url="https://x")


VALID_ASSET = {
    "schema": 1,
    "apps": [
        {
            "name": "palmimo-teleop",
            "description": "d",
            "source": {
                "type": "git",
                "url": "https://x",
                "ref_kind": "tag",
                "ref": "v1.0.0",
                "commit": "deadbeef",
            },
            "env": {},
            "devices": ["camera"],
        }
    ],
}


def _opener_for(asset_bytes: bytes) -> Any:
    sha_line = f"{hashlib.sha256(asset_bytes).hexdigest()}  palmimo-catalog-v1.0.0.json\n".encode()

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        if request.full_url.endswith(".sha256"):
            return _FakeResponse(sha_line)
        return _FakeResponse(asset_bytes)

    return opener


def test_fetch_parses_a_valid_catalog_asset() -> None:
    asset_bytes = json.dumps(VALID_ASSET).encode("utf-8")
    source = GitHubCatalogSource(
        catalog_repo="Jizai-inc/palmimo-devkit", release_source=_FakeReleaseSource(), opener=_opener_for(asset_bytes)
    )

    asset = source.fetch()

    assert asset.tag == "v1.0.0"
    assert asset.apps == (
        CatalogApp(
            name="palmimo-teleop",
            description="d",
            source=AppSource(type="git", url="https://x", ref_kind="tag", ref="v1.0.0", commit="deadbeef"),
            env={},
            devices=("camera",),
        ),
    )


def test_fetch_parses_a_non_default_manifest_filename_on_source() -> None:
    payload = json.loads(json.dumps(VALID_ASSET))
    payload["apps"][0]["source"]["manifest"] = "palmimo.realtime.toml"
    asset_bytes = json.dumps(payload).encode("utf-8")
    source = GitHubCatalogSource(
        catalog_repo="Jizai-inc/palmimo-devkit", release_source=_FakeReleaseSource(), opener=_opener_for(asset_bytes)
    )

    asset = source.fetch()

    assert asset.apps[0].source.manifest == "palmimo.realtime.toml"


def test_fetch_rejects_an_official_catalog_source_without_a_commit_pin() -> None:
    payload = json.loads(json.dumps(VALID_ASSET))
    del payload["apps"][0]["source"]["commit"]
    asset_bytes = json.dumps(payload).encode("utf-8")
    source = GitHubCatalogSource(
        catalog_repo="Jizai-inc/palmimo-devkit", release_source=_FakeReleaseSource(), opener=_opener_for(asset_bytes)
    )

    with pytest.raises(CatalogSourceError, match="commit"):
        source.fetch()


@pytest.mark.parametrize(
    ("help_url", "expected"),
    [("https://docs.example.com/token", "https://docs.example.com/token"), ("javascript:alert(1)", None)],
    ids=["valid_help_url_exposed", "invalid_scheme_dropped"],
)
def test_fetch_parses_env_help_url(help_url: str, expected: str | None) -> None:
    payload = json.loads(json.dumps(VALID_ASSET))
    payload["apps"][0]["env"] = {"TOKEN": {"required": True, "description": "d", "help_url": help_url}}
    asset_bytes = json.dumps(payload).encode("utf-8")
    source = GitHubCatalogSource(
        catalog_repo="Jizai-inc/palmimo-devkit", release_source=_FakeReleaseSource(), opener=_opener_for(asset_bytes)
    )

    asset = source.fetch()

    assert asset.apps[0].env == {"TOKEN": CatalogEnvSpec(required=True, description="d", help_url=expected)}


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 2, "apps": []},
        {"schema": 1, "apps": "not-a-list"},
        {"schema": 1, "apps": [{"name": "x"}]},  # missing description/source/env/devices
        {
            "schema": 1,
            "apps": [
                {
                    "name": "palmimo-teleop",
                    "description": "d",
                    "source": {
                        "type": "git",
                        "url": "https://x",
                        "ref_kind": "tag",
                        "ref": "v1.0.0",
                        "manifest": "bad name",
                    },
                    "env": {},
                    "devices": ["camera"],
                }
            ],
        },
    ],
    ids=["wrong_schema_version", "apps_not_a_list", "app_missing_required_field", "app_invalid_manifest_filename"],
)
def test_fetch_rejects_an_asset_with_an_invalid_shape(payload: dict) -> None:
    asset_bytes = json.dumps(payload).encode("utf-8")
    source = GitHubCatalogSource(
        catalog_repo="Jizai-inc/palmimo-devkit", release_source=_FakeReleaseSource(), opener=_opener_for(asset_bytes)
    )

    with pytest.raises(CatalogSourceError):
        source.fetch()


def test_fetch_rejects_a_checksum_mismatch() -> None:
    asset_bytes = json.dumps(VALID_ASSET).encode("utf-8")

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        if request.full_url.endswith(".sha256"):
            return _FakeResponse(b"0" * 64 + b"  palmimo-catalog-v1.0.0.json\n")
        return _FakeResponse(asset_bytes)

    source = GitHubCatalogSource(
        catalog_repo="Jizai-inc/palmimo-devkit", release_source=_FakeReleaseSource(), opener=opener
    )

    with pytest.raises(CatalogSourceError):
        source.fetch()
