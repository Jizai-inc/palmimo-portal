"""Behavioral tests for the schema=1 palmimo.toml parser (design doc chapter 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_portal.core.manifest import (
    ManifestValidationError,
    parse_manifest,
    resolve_command,
    resolve_url,
)


FIXTURES = Path(__file__).parent / "fixtures" / "manifests"
VALID_FIXTURES = sorted((FIXTURES / "valid").glob("*.toml"))
INVALID_FIXTURES = sorted((FIXTURES / "invalid").glob("*.toml"))


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.stem)
def test_parse_manifest_accepts_example_fixture(path: Path) -> None:
    manifest = parse_manifest(path.read_text())
    assert manifest.name


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.stem)
def test_parse_manifest_rejects_invalid_fixture(path: Path) -> None:
    with pytest.raises(ManifestValidationError) as excinfo:
        parse_manifest(path.read_text())
    assert excinfo.value.errors


def test_parse_manifest_reports_every_error_at_once() -> None:
    # unknown top-level key AND bad name pattern in the same document: a single
    # validation pass should surface both errors, not stop at the first.
    text = """
    schema = 1
    name = "BadName"
    description = "x"
    command = ["run"]
    unsupported = true
    """
    with pytest.raises(ManifestValidationError) as excinfo:
        parse_manifest(text)
    assert len(excinfo.value.errors) >= 2


def test_resolve_command_substitutes_bool_true_element() -> None:
    manifest = parse_manifest(
        """
        schema = 1
        name = "app"
        description = "x"
        command = ["run", "{verbose}"]

        [params.verbose]
        type = "bool"
        default = false
        flag = "--verbose"
        """
    )
    argv = resolve_command(manifest, {"verbose": True}, host="pi.local", app_dir="/var/lib/palmimo/apps/app")
    assert argv == ["run", "--verbose"]


def test_resolve_command_drops_element_for_bool_false() -> None:
    manifest = parse_manifest(
        """
        schema = 1
        name = "app"
        description = "x"
        command = ["run", "{verbose}"]

        [params.verbose]
        type = "bool"
        default = false
        flag = "--verbose"
        """
    )
    argv = resolve_command(manifest, {"verbose": False}, host="pi.local", app_dir="/var/lib/palmimo/apps/app")
    assert argv == ["run"]


def test_resolve_command_substitutes_host_from_request() -> None:
    manifest = parse_manifest(
        """
        schema = 1
        name = "app"
        description = "x"
        command = ["run"]
        url = "http://{host}:{port}/"

        [params.port]
        type = "int"
        default = 8000
        """
    )
    url = resolve_url(manifest, {"port": 8000}, host="palmimo-abc123.local", app_dir="/x")
    assert url == "http://palmimo-abc123.local:8000/"


def test_parse_manifest_rejects_undefined_placeholder_in_url() -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            """
            schema = 1
            name = "app"
            description = "x"
            command = ["run"]
            url = "http://{host}:{missing}/"
            """
        )


def test_parse_manifest_rejects_mixed_element_for_bool_placeholder() -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            """
            schema = 1
            name = "app"
            description = "x"
            command = ["run", "--x={verbose}"]

            [params.verbose]
            type = "bool"
            default = false
            flag = "--verbose"
            """
        )


def test_resolve_command_rejects_enum_value_outside_choices() -> None:
    manifest = parse_manifest(
        """
        schema = 1
        name = "app"
        description = "x"
        command = ["run", "{camera}"]

        [params.camera]
        type = "enum"
        choices = ["head", "wide"]
        default = "head"
        """
    )
    with pytest.raises(ManifestValidationError):
        resolve_command(manifest, {"camera": "back"}, host="h", app_dir="/x")


def test_resolve_command_uses_default_when_param_unset() -> None:
    manifest = parse_manifest(
        """
        schema = 1
        name = "app"
        description = "x"
        command = ["run", "--port", "{port}"]

        [params.port]
        type = "int"
        default = 8000
        """
    )
    argv = resolve_command(manifest, {}, host="h", app_dir="/x")
    assert argv == ["run", "--port", "8000"]


def test_resolve_command_raises_when_required_param_missing() -> None:
    manifest = parse_manifest(
        """
        schema = 1
        name = "app"
        description = "x"
        command = ["run", "--port", "{port}"]

        [params.port]
        type = "int"
        """
    )
    with pytest.raises(ManifestValidationError):
        resolve_command(manifest, {}, host="h", app_dir="/x")


@pytest.mark.parametrize("element", ["{foo-bar}", "{}"])
def test_parse_manifest_rejects_placeholder_with_invalid_name(element: str) -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            f"""
            schema = 1
            name = "app"
            description = "x"
            command = ["run", "{element}"]
            """
        )


def test_parse_manifest_rejects_pattern_that_fails_to_compile() -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            """
            schema = 1
            name = "app"
            description = "x"
            command = ["run", "{value}"]

            [params.value]
            type = "string"
            pattern = "[unclosed"
            """
        )


def test_parse_manifest_rejects_pattern_with_nested_quantifier() -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            """
            schema = 1
            name = "app"
            description = "x"
            command = ["run", "{value}"]

            [params.value]
            type = "string"
            pattern = "(a+)+"
            """
        )


def test_resolve_command_substitutes_app_dir() -> None:
    manifest = parse_manifest(
        """
        schema = 1
        name = "app"
        description = "x"
        command = ["run", "--root", "{app_dir}"]
        """
    )
    argv = resolve_command(manifest, {}, host="h", app_dir="/var/lib/palmimo/apps/app")
    assert argv == ["run", "--root", "/var/lib/palmimo/apps/app"]
