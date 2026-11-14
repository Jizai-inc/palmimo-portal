"""Translations remain display-only through loading, catalog caching, and API schemas."""

import json
from pathlib import Path

import pytest

from palmimo_portal.api.apps import ManifestPreviewResponse
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


def test_asset_translations_remain_display_only_after_include_expansion(tmp_path: Path) -> None:
    (tmp_path / "palmimo.toml").write_text(
        'schema=2\nname="face"\ndescription="Original"\ncommand=["run"]\n[assets]\ninclude="more.toml"'
    )
    (tmp_path / "more.toml").write_text(
        '[assets.face]\nfile="face.json"\ndescription="Face"\n[assets.face.params.eye_color]\ndescription="Eye color"'
    )
    (tmp_path / "face.json").write_text("{}")
    translations = {"ja": {"assets.face": "Translated face", "assets.face.params.eye_color": "Translated color"}}
    (tmp_path / "palmimo.toml.i18n.json").write_text(json.dumps({"schema": 1, "translations": translations}))
    manifest = _read_manifest(tmp_path)
    assert manifest.translations == translations
    assert manifest.assets["face"].description == "Face"
    assert manifest.assets["face"].params == {"eye_color": "Eye color"}
    assert "translations" not in manifest_snapshot(manifest)
    assert resolve_command(manifest, {}, host="robot", app_dir="/app") == ["run"]
