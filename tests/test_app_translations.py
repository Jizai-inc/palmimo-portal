"""Translations remain display-only through loading, catalog caching, and API schemas."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from palmimo_portal.adapters.catalog import _parse_catalog
from palmimo_portal.adapters.state import JsonFileStateStore
from palmimo_portal.api.apps import ManifestPreviewResponse
from palmimo_portal.api.catalog import CatalogAppInfo
from palmimo_portal.core.app_translations import parse_translations
from palmimo_portal.core.apps_jobs import _read_manifest
from palmimo_portal.core.manifest import manifest_snapshot, parse_manifest, resolve_command


MANIFEST = 'schema=1\nname="sample"\ndescription="Original"\ncommand=["run", "{port}"]\n[params.port]\ntype="int"\ndefault=8000\ndescription="Port"\n'
TRANSLATIONS = {"ja": {"description": "Translated summary", "params.port": "Translated port"}}


def test_manifest_translation_does_not_change_commands_or_ledger(tmp_path: Path) -> None:
    (tmp_path / "palmimo.realtime.toml").write_text(MANIFEST)
    (tmp_path / "palmimo.realtime.toml.i18n.json").write_text(json.dumps({"schema": 1, "translations": TRANSLATIONS}))
    (tmp_path / "palmimo.toml.i18n.json").write_text(
        '{"schema":1,"translations":{"ja":{"description":"Other variant"}}}'
    )
    manifest = _read_manifest(tmp_path, "palmimo.realtime.toml")
    assert manifest.translations == TRANSLATIONS
    assert manifest_snapshot(manifest) == manifest_snapshot(parse_manifest(MANIFEST))
    assert resolve_command(manifest, {}, host="robot", app_dir="/app") == ["run", "8000"]
    response = ManifestPreviewResponse(
        name=manifest.name,
        namespace="zip",
        suggested_name=manifest.name,
        suggested_id="zip.sample",
        description=manifest.description,
        devices=[],
        env=[],
        translations=manifest.translations,
    )
    assert response.model_dump()["translations"] == TRANSLATIONS


@pytest.mark.parametrize("content", [b"not json", b"\xff", b"x" * 65_537, b'{"schema":2,"translations":{}}'])
def test_bad_optional_translation_keeps_app_usable(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "palmimo.toml").write_text(MANIFEST)
    (tmp_path / "palmimo.toml.i18n.json").write_bytes(content)
    assert _read_manifest(tmp_path).translations == {}


def test_missing_or_symlink_translation_keeps_original_text(tmp_path: Path) -> None:
    (tmp_path / "palmimo.toml").write_text(MANIFEST)
    assert _read_manifest(tmp_path).translations == {}
    target = tmp_path / "untrusted.json"
    target.write_text(json.dumps({"schema": 1, "translations": TRANSLATIONS}))
    (tmp_path / "palmimo.toml.i18n.json").symlink_to(target)
    assert _read_manifest(tmp_path).translations == {}


def test_translation_parser_discards_invalid_fields_without_discarding_valid_ones() -> None:
    assert parse_translations(
        {
            "ja": {"description": "Valid", "command": "rm", "env.KEY": 42, "params.port": " "},
            "JA": {"description": "Invalid locale"},
        }
    ) == {"ja": {"description": "Valid"}}


def test_catalog_translations_survive_cache_and_response() -> None:
    entry = {
        "name": "sample",
        "description": "Original",
        "env": {},
        "devices": [],
        "source": {
            "type": "git",
            "url": "https://github.com/Jizai-inc/palmimo-devkit",
            "ref_kind": "tag",
            "ref": "examples-v1",
            "commit": "abc",
        },
        "translations": TRANSLATIONS,
    }
    (app,) = _parse_catalog(json.dumps({"schema": 1, "apps": [entry]}).encode(), "catalog.json")
    cached = JsonFileStateStore._parse_catalog_cache(
        json.dumps({"schema": 1, "tag": "examples-v1", "fetched_at": 1, "apps": [asdict(app)]})
    )
    assert cached.apps[0].translations == TRANSLATIONS
    source = {**asdict(app.source), "official": True}
    response = CatalogAppInfo.model_validate(
        {
            "name": app.name,
            "description": app.description,
            "source": source,
            "env": [],
            "devices": [],
            "translations": app.translations,
        }
    )
    assert response.model_dump()["translations"] == TRANSLATIONS
