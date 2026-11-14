"""Behavioral tests for app manifest validation and parameter resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_portal.core.manifest import (
    InvalidManifestFilenameError,
    ManifestValidationError,
    manifest_from_snapshot,
    manifest_snapshot,
    parse_manifest,
    resolve_command,
    resolve_url,
    validate_manifest_filename,
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


def test_manifest_from_snapshot_round_trips_a_control_character_in_a_string_value() -> None:
    manifest = parse_manifest(
        'schema = 1\nname = "app"\ndescription = "d"\ncommand = ["run"]\n\n'
        '[env.API_KEY]\ndescription = "has a \\u007f delete char"\n'
    )
    snapshot = manifest_snapshot(manifest)

    restored = manifest_from_snapshot(snapshot)

    assert restored.env["API_KEY"].description == "has a \x7f delete char"


@pytest.mark.parametrize("field", ["default", "min", "max"])
@pytest.mark.parametrize("literal", ["inf", "-inf", "nan"])
def test_parse_manifest_rejects_a_non_finite_param_bound(field: str, literal: str) -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            'schema = 1\nname = "app"\ndescription = "d"\ncommand = ["run"]\n\n'
            f'[params.speed]\ntype = "float"\n{field} = {literal}\n'
        )


def test_manifest_from_snapshot_rejects_a_non_finite_param_default() -> None:
    snapshot = {
        "schema": 1,
        "name": "app",
        "description": "d",
        "command": ["run"],
        "url": None,
        "devices": [],
        "env": {},
        "params": {"speed": {"type": "float", "default": float("inf")}},
    }

    with pytest.raises(ManifestValidationError):
        manifest_from_snapshot(snapshot)


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


def test_parse_manifest_rejects_pattern_with_alternation() -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            """
            schema = 1
            name = "app"
            description = "x"
            command = ["run", "{value}"]

            [params.value]
            type = "string"
            pattern = "(a|aa)+$"
            """
        )


def test_parse_manifest_accepts_a_256_character_pattern() -> None:
    pattern = "a" * 256
    manifest = parse_manifest(
        f'''\
        schema = 1
        name = "app"
        description = "x"
        command = ["run", "{{value}}"]

        [params.value]
        type = "string"
        pattern = "{pattern}"
        '''
    )

    assert manifest.params["value"].pattern == pattern


@pytest.mark.parametrize("name", [None, "", "palmimo.toml", "palmimo.realtime.toml"])
def test_validate_manifest_filename_accepts_default_and_variant_names(name: str | None) -> None:
    result = validate_manifest_filename(name)
    assert result == (name if name else "palmimo.toml")


@pytest.mark.parametrize(
    "name",
    ["palmimo_backup.toml", "palmimo.TOML", "palmimo..toml", "../palmimo.toml", "palmimo.toml.bak", "not-palmimo.toml"],
)
def test_validate_manifest_filename_rejects_names_outside_the_shape(name: str) -> None:
    with pytest.raises(InvalidManifestFilenameError):
        validate_manifest_filename(name)


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


ASSET_MANIFEST = 'schema=2\nname="face"\ndescription="Face"\ncommand=["run"]\n'


def test_manifest_assets_expand_include_and_survive_snapshot() -> None:
    text = (
        ASSET_MANIFEST
        + """
[assets]
include = "assets/more.toml"
[assets.welcome]
file = "assets/welcome.json"
description = "Welcome"
tags = ["greeting"]
[assets.welcome.params.eye_color]
description = "Eye color"
"""
    )
    manifest = parse_manifest(
        text,
        read_include=lambda path: (
            """
[assets.bye]
file = "assets/bye.gif"
description = "Goodbye"
"""
        ),
    )
    restored = manifest_from_snapshot(manifest_snapshot(manifest))
    assert restored.assets == manifest.assets
    assert set(restored.assets) == {"welcome", "bye"}
    assert restored.assets["welcome"].file == "assets/welcome.json"
    assert restored.assets["welcome"].description == "Welcome"
    assert restored.assets["welcome"].tags == ("greeting",)
    assert restored.assets["welcome"].params == {"eye_color": "Eye color"}
    assert restored.assets["bye"].tags == ()


@pytest.mark.parametrize(
    "declaration",
    [
        'assets = "bad"',
        '[assets.include]\nfile="a.json"\ndescription="d"',
        '[assets.a]\nfile="a.txt"\ndescription="d"',
        '[assets.a]\nfile="a.png"\ndescription="d"\n[assets.a.params]',
        '[assets.a]\nfile="a.json"\ndescription="d"\n[assets.a.params.eye]\ndescription=42',
        '[assets.a]\nfile="a.json"\ndescription="d"\n[assets.a.params.eye]\ndescription="d"\nextra=true',
        '[assets."Bad"]\nfile="a.json"\ndescription="d"',
        '[assets.a]\nfile="a.json"\ndescription="d"\nextra=true',
        '[assets.a]\ndescription="d"',
        '[assets.a]\nfile="a.json"',
        '[assets.a]\nfile=42\ndescription="d"',
        '[assets.a]\nfile="/a.json"\ndescription="d"',
        '[assets.a]\nfile="sub/../a.json"\ndescription="d"',
        '[assets.a]\nfile="a.json"\ndescription=""',
        '[assets.a]\nfile="a.json"\ndescription=42',
        '[assets.a]\nfile="a.json"\ndescription="' + "d" * 201 + '"',
        '[assets.a]\nfile="a.json"\ndescription="d"\ntags="greeting"',
        '[assets.a]\nfile="a.json"\ndescription="d"\ntags=["Bad"]',
        '[assets.a]\nfile="a.json"\ndescription="d"\ntags=[42]',
        '[assets.a]\nfile="a.json"\ndescription="d"\nparams="bad"',
        '[assets.a]\nfile="a.json"\ndescription="d"\n[assets.a.params.eye]\n',
        '[assets.a]\nfile="a.json"\ndescription="d"\n[assets.a.params."Bad"]\ndescription="d"',
        '[assets.a]\nfile="a.json"\ndescription="d"\n[assets.a.params.eye]\ndescription=""',
        '[assets.a]\nfile="a.png"\ndescription="d"\n[assets.a.params.eye]\ndescription="d"',
    ],
)
def test_manifest_assets_reject_invalid_declarations(declaration: str) -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(ASSET_MANIFEST + declaration)


@pytest.mark.parametrize(
    "include",
    [
        "[extra]\na=1",
        '[assets]\ninclude="nested.toml"',
        '[assets.welcome]\nfile="other.json"\ndescription="other"',
        "invalid TOML",
        'assets="bad"',
    ],
)
def test_manifest_assets_reject_invalid_include(include: str) -> None:
    text = ASSET_MANIFEST + '[assets]\ninclude="more.toml"\n[assets.welcome]\nfile="a.json"\ndescription="d"'
    with pytest.raises(ManifestValidationError):
        parse_manifest(text, read_include=lambda path: include)


@pytest.mark.parametrize("path", ["/more.toml", "../more.toml", "sub/../more.toml", ""])
def test_manifest_assets_reject_unsafe_include_without_reading(path: str) -> None:
    def read_include(path: str) -> str:
        pytest.fail("unsafe include must not be read")

    with pytest.raises(ManifestValidationError):
        parse_manifest(ASSET_MANIFEST + f'[assets]\ninclude="{path}"', read_include=read_include)


def test_manifest_snapshot_rejects_unexpanded_include() -> None:
    snapshot = manifest_snapshot(parse_manifest(ASSET_MANIFEST))
    snapshot["assets"] = {"include": "more.toml"}
    with pytest.raises(ManifestValidationError):
        manifest_from_snapshot(snapshot)


def test_manifest_schema_one_rejects_assets() -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(ASSET_MANIFEST.replace("schema=2", "schema=1") + "[assets]\n")


def test_manifest_future_schema_has_distinct_error() -> None:
    from palmimo_portal.core.manifest import ManifestSchemaUnsupportedError

    with pytest.raises(ManifestSchemaUnsupportedError) as caught:
        parse_manifest(ASSET_MANIFEST.replace("schema=2", "schema=3"))
    assert caught.value.schema == 3
    assert caught.value.code == "manifest_schema_unsupported"


@pytest.mark.parametrize(
    "source", ["missing", "absolute", "parent", "symlink", "symlink-parent", "oversized", "invalid-utf8"]
)
def test_manifest_loading_rejects_unsafe_include_files(tmp_path: Path, source: str) -> None:
    from palmimo_portal.core.apps_jobs import _read_manifest

    include_path = "more.toml"
    content = b'[assets.bye]\nfile="bye.gif"\ndescription="d"'
    if source == "absolute":
        include_path = str(tmp_path / "more.toml")
    elif source == "parent":
        include_path = "../more.toml"
    elif source == "symlink":
        (tmp_path / "target.toml").write_bytes(content)
        (tmp_path / "more.toml").symlink_to(tmp_path / "target.toml")
    elif source == "symlink-parent":
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "more.toml").write_bytes(content)
        (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
        include_path = "linked/more.toml"
    elif source == "oversized":
        (tmp_path / "more.toml").write_bytes(content + b"\n#" + b"x" * 65536)
    elif source == "invalid-utf8":
        (tmp_path / "more.toml").write_bytes(b"\xff")
    (tmp_path / "palmimo.toml").write_text(ASSET_MANIFEST + f'[assets]\ninclude="{include_path}"')
    with pytest.raises(ManifestValidationError):
        _read_manifest(tmp_path)


@pytest.mark.parametrize("padded", [False, True])
def test_manifest_loading_expands_include_relative_to_app_directory(tmp_path: Path, padded: bool) -> None:
    from palmimo_portal.core.apps_jobs import _read_manifest

    (tmp_path / "declarations").mkdir()
    content = '[assets.bye]\nfile="bye.gif"\ndescription="d"'
    if padded:
        content = content.ljust(65_536)
    (tmp_path / "declarations" / "more.toml").write_text(content)
    (tmp_path / "bye.gif").write_bytes(b"gif")
    (tmp_path / "palmimo.variant.toml").write_text(ASSET_MANIFEST + '[assets]\ninclude="declarations/more.toml"')
    assert _read_manifest(tmp_path, "palmimo.variant.toml").assets["bye"].file == "bye.gif"


@pytest.mark.parametrize("included", [False, True])
@pytest.mark.parametrize("directory_link", [False, True])
def test_manifest_loading_rejects_symlink_asset_files(tmp_path: Path, included: bool, directory_link: bool) -> None:
    from palmimo_portal.core.apps_jobs import _read_manifest

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "face.json").write_text("{}")
    if directory_link:
        (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
        asset_path = "linked/face.json"
    else:
        (tmp_path / "face.json").symlink_to(tmp_path / "real" / "face.json")
        asset_path = "face.json"
    declaration = f'[assets.face]\nfile="{asset_path}"\ndescription="d"'
    if included:
        (tmp_path / "more.toml").write_text(declaration)
        declaration = '[assets]\ninclude="more.toml"'
    (tmp_path / "palmimo.toml").write_text(ASSET_MANIFEST + declaration)
    with pytest.raises(ManifestValidationError):
        _read_manifest(tmp_path)


@pytest.mark.parametrize("schema", [True, 1.0, "2", None])
def test_manifest_rejects_non_integer_schema(schema: object) -> None:
    snapshot = manifest_snapshot(parse_manifest(ASSET_MANIFEST))
    snapshot["schema"] = schema
    with pytest.raises(ManifestValidationError):
        manifest_from_snapshot(snapshot)
