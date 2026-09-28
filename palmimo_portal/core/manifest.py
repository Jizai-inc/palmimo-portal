"""Parser and validator for the schema=1 ``palmimo.toml`` app manifest.

Portal is the only reader of this format (design doc "app-platform" ch. 1);
devkit only ships the spec text and example files. Every top-level key is
declared here on purpose -- an unknown key is rejected rather than ignored,
so a typo in a manifest cannot silently fail to take effect.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from typing import Any, Literal, cast


_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
#: A directory may ship several manifests, one app each: the default ``palmimo.toml`` or a
#: variant ``palmimo.<variant>.toml`` with the same ``<variant>`` shape as an app ``name``.
MANIFEST_FILENAME_RE = re.compile(r"^palmimo(\.[a-z][a-z0-9-]{0,39})?\.toml$")
DEFAULT_MANIFEST_FILENAME = "palmimo.toml"
_PARAM_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_RESERVED_ENV_PREFIX = "PALMIMO_"
_KNOWN_DEVICES = frozenset({"camera", "audio", "motor_display"})
_RESERVED_PLACEHOLDERS = frozenset({"host", "app_dir"})
_TOP_LEVEL_KEYS = frozenset({"schema", "name", "description", "command", "url", "devices", "env", "params"})
_ENV_TABLE_KEYS = frozenset({"required", "description", "help_url"})
_PARAM_TYPES = frozenset({"string", "int", "float", "enum", "bool"})
# Extra keys each param type accepts beyond {type, default}.
_PARAM_TYPE_EXTRA_KEYS: dict[str, frozenset[str]] = {
    "string": frozenset({"pattern", "max_length"}),
    "int": frozenset({"min", "max"}),
    "float": frozenset({"min", "max"}),
    "enum": frozenset({"choices"}),
    "bool": frozenset({"flag"}),
}
_DEFAULT_MAX_LENGTH = 256
_MAX_PATTERN_LENGTH = 256
# Any brace-delimited text is a placeholder candidate; name validity against
# _PARAM_NAME_PATTERN is checked separately so `{}` and `{foo-bar}` are
# reported as undefined rather than silently ignored by this regex.
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
#: Flags a group that is itself quantified and whose own contents are
#: quantified, e.g. ``(a+)+`` or ``(\d*)*`` -- catastrophic-backtracking
#: shapes. A simple scan, not a general parse: nested groups two levels deep
#: aren't caught, but no manifest needs that.
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")

#: Sentinel distinguishing "no default given" (param is required) from a default of
#: e.g. ``False`` or ``0``, which are falsy but present.
_NO_DEFAULT = object()

ParamType = Literal["string", "int", "float", "enum", "bool"]


def _is_safe_pattern(pattern: str) -> bool:
    in_class = False
    escaped = False
    for char in pattern:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        elif not in_class and char in "()|":
            return False
    return not escaped and not in_class


class InvalidManifestFilenameError(ValueError):
    """Raised by :func:`validate_manifest_filename`.

    A :class:`ValueError` subclass, like ``core.apps.InvalidGitSourceError``, so a pydantic
    field validator can raise it directly and get an automatic 422.
    """


def validate_manifest_filename(name: str | None) -> str:
    """Return ``name`` if it names a manifest a directory may ship, or :data:`DEFAULT_MANIFEST_FILENAME` for ``None``/``""``.

    Raises:
        InvalidManifestFilenameError: ``name`` is neither ``palmimo.toml`` nor
            ``palmimo.<variant>.toml`` (``<variant>`` matching an app ``name``'s own shape).
    """
    if not name:
        return DEFAULT_MANIFEST_FILENAME
    if not MANIFEST_FILENAME_RE.fullmatch(name):
        raise InvalidManifestFilenameError(f"manifest filename must match {MANIFEST_FILENAME_RE.pattern}, got {name!r}")
    return name


class ManifestValidationError(Exception):
    """All validation failures for one manifest, collected in a single pass."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class EnvSpec:
    required: bool
    description: str
    help_url: str | None = None


@dataclass(frozen=True)
class ParamSpec:
    type: ParamType
    default: Any = _NO_DEFAULT
    min: float | None = None
    max: float | None = None
    choices: list[str] | None = None
    pattern: str | None = None
    max_length: int = _DEFAULT_MAX_LENGTH
    flag: str | None = None
    description: str | None = None

    @property
    def has_default(self) -> bool:
        return self.default is not _NO_DEFAULT


@dataclass(frozen=True)
class Manifest:
    schema: int
    name: str
    description: str
    command: tuple[str, ...]
    url: str | None
    devices: frozenset[str]
    env: dict[str, EnvSpec]
    params: dict[str, ParamSpec]


def manifest_snapshot(manifest: Manifest) -> dict[str, Any]:
    """Return the validated, JSON-safe manifest form stored in the app ledger."""
    params: dict[str, dict[str, Any]] = {}
    for name, spec in manifest.params.items():
        value: dict[str, Any] = {"type": spec.type}
        if spec.has_default:
            value["default"] = spec.default
        if spec.min is not None:
            value["min"] = spec.min
        if spec.max is not None:
            value["max"] = spec.max
        if spec.choices is not None:
            value["choices"] = list(spec.choices)
        if spec.pattern is not None:
            value["pattern"] = spec.pattern
        if spec.type == "string" and spec.max_length != _DEFAULT_MAX_LENGTH:
            value["max_length"] = spec.max_length
        if spec.flag is not None:
            value["flag"] = spec.flag
        if spec.description is not None:
            value["description"] = spec.description
        params[name] = value
    return {
        "schema": manifest.schema,
        "name": manifest.name,
        "description": manifest.description,
        "command": list(manifest.command),
        "url": manifest.url,
        "devices": sorted(manifest.devices),
        "env": {
            name: {
                "required": spec.required,
                "description": spec.description,
                **({"help_url": spec.help_url} if spec.help_url is not None else {}),
            }
            for name, spec in manifest.env.items()
        },
        "params": params,
    }


def manifest_from_snapshot(snapshot: Any) -> Manifest:
    """Validate and restore a ledger manifest snapshot.

    Validates ``snapshot`` directly as the small dict shape
    :func:`manifest_snapshot` produces, sharing every rule with
    :func:`parse_manifest` -- ``apps.json`` is operator-writable state, so
    treating its snapshot as already trusted would let a tampered ledger
    grant a new device class at start time. Deliberately does not round-trip
    through TOML text: a JSON-valid string (a control character like
    U+007F, or a non-ASCII codepoint) can be invalid TOML syntax, which
    would reject a snapshot :func:`manifest_snapshot` itself produced.
    """
    if not isinstance(snapshot, dict):
        raise ManifestValidationError(["manifest snapshot must be an object"])
    return _validate_manifest_dict(snapshot)


def parse_manifest(text: str) -> Manifest:
    """Parse and fully validate a ``palmimo.toml`` document.

    Raises :class:`ManifestValidationError` carrying every violation found,
    not just the first, so a manifest author fixes the file in one pass.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ManifestValidationError([f"invalid TOML: {error}"]) from error
    return _validate_manifest_dict(raw)


def _validate_manifest_dict(raw: dict[str, Any]) -> Manifest:
    """Validate the small manifest schema against an already-parsed dict (TOML or a ledger snapshot)."""
    errors: list[str] = []
    unknown_keys = set(raw) - _TOP_LEVEL_KEYS
    for key in sorted(unknown_keys):
        errors.append(f"unknown top-level key: {key!r}")

    schema = raw.get("schema")
    if schema != 1:
        errors.append(f"schema must be 1, got {schema!r}")

    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        errors.append(f"name must match {_NAME_PATTERN.pattern}, got {name!r}")

    description = raw.get("description")
    if not isinstance(description, str) or not (1 <= len(description) <= 200):
        errors.append("description must be a string of 1-200 characters")

    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        errors.append("command must be a non-empty array of strings")
        command = []

    url = raw.get("url")
    if url is not None and not _is_http_url(url):
        errors.append("url must start with http:// or https://")

    devices_raw = raw.get("devices", [])
    devices: frozenset[str] = frozenset()
    if not isinstance(devices_raw, list) or not all(isinstance(item, str) for item in devices_raw):
        errors.append("devices must be an array of strings")
    else:
        unknown_devices = set(devices_raw) - _KNOWN_DEVICES
        for device in sorted(unknown_devices):
            errors.append(f"unknown device: {device!r}")
        devices = frozenset(devices_raw) & _KNOWN_DEVICES

    env = _parse_env(raw.get("env", {}), errors)
    params = _parse_params(raw.get("params", {}), errors)

    _validate_placeholders(command, "command", params, errors)
    if isinstance(url, str):
        _validate_placeholders([url], "url", params, errors)

    if errors:
        raise ManifestValidationError(errors)

    return Manifest(
        schema=cast(int, schema),
        name=cast(str, name),
        description=cast(str, description),
        command=tuple(command),
        url=cast("str | None", url),
        devices=devices,
        env=env,
        params=params,
    )


def _parse_env(raw: Any, errors: list[str]) -> dict[str, EnvSpec]:
    env: dict[str, EnvSpec] = {}
    if not isinstance(raw, dict):
        errors.append("env must be a table")
        return env
    for name, spec in raw.items():
        if not _ENV_NAME_PATTERN.fullmatch(name):
            errors.append(f"env name must match {_ENV_NAME_PATTERN.pattern}, got {name!r}")
            continue
        if name.startswith(_RESERVED_ENV_PREFIX):
            errors.append(f"env name {name!r} uses the reserved {_RESERVED_ENV_PREFIX} prefix")
            continue
        if not isinstance(spec, dict):
            errors.append(f"env.{name} must be a table")
            continue
        unknown_env_keys = set(spec) - _ENV_TABLE_KEYS
        if unknown_env_keys:
            errors.append(f"env.{name} has unknown key(s): {sorted(unknown_env_keys)}")
            continue
        description = spec.get("description")
        if not isinstance(description, str) or not (1 <= len(description) <= 200):
            errors.append(f"env.{name}.description must be a string of 1-200 characters")
            continue
        required = spec.get("required", True)
        if not isinstance(required, bool):
            errors.append(f"env.{name}.required must be a bool")
            continue
        help_url = spec.get("help_url")
        if help_url is not None and not _is_http_url(help_url):
            errors.append(f"env.{name}.help_url must start with http:// or https://")
            continue
        env[name] = EnvSpec(required=required, description=description, help_url=help_url)
    return env


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and (value.startswith("http://") or value.startswith("https://"))


def _is_number_not_bool(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    # Both TOML (`inf`, `nan`, `-inf`) and JSON (via Python's own non-standard
    # `json.loads` extension) can hand a non-finite float to a default/min/max.
    return _is_number_not_bool(value) and math.isfinite(value)


def _has_nested_quantifier(pattern: str) -> bool:
    return _NESTED_QUANTIFIER_RE.search(pattern) is not None


def _parse_params(raw: Any, errors: list[str]) -> dict[str, ParamSpec]:
    params: dict[str, ParamSpec] = {}
    if not isinstance(raw, dict):
        errors.append("params must be a table")
        return params
    for name, spec in raw.items():
        if not _PARAM_NAME_PATTERN.fullmatch(name):
            errors.append(f"param name must match {_PARAM_NAME_PATTERN.pattern}, got {name!r}")
            continue
        if not isinstance(spec, dict):
            errors.append(f"params.{name} must be a table")
            continue
        param_type = spec.get("type")
        if param_type not in _PARAM_TYPES:
            errors.append(f"params.{name}.type must be one of {sorted(_PARAM_TYPES)}, got {param_type!r}")
            continue
        allowed_keys = {"type", "default", "description"} | _PARAM_TYPE_EXTRA_KEYS[param_type]
        unknown_param_keys = set(spec) - allowed_keys
        if unknown_param_keys:
            errors.append(f"params.{name} has unknown key(s) for type {param_type!r}: {sorted(unknown_param_keys)}")
            continue
        description = spec.get("description")
        if description is not None and not isinstance(description, str):
            errors.append(f"params.{name}.description must be a string")
            continue
        default = spec.get("default", _NO_DEFAULT)
        param = _parse_param_by_type(name, param_type, spec, default, description, errors)
        if param is None:
            continue
        if param.has_default:
            _validate_param_value(name, param, param.default, errors)
        params[name] = param
    return params


def _parse_param_by_type(
    name: str, param_type: str, spec: dict[str, Any], default: Any, description: str | None, errors: list[str]
) -> ParamSpec | None:
    if param_type == "bool":
        flag = spec.get("flag")
        if not isinstance(flag, str) or not flag:
            errors.append(f"params.{name} (type bool) requires a non-empty 'flag'")
            return None
        return ParamSpec(type="bool", default=default, flag=flag, description=description)

    if param_type == "enum":
        choices = spec.get("choices")
        if not isinstance(choices, list) or not choices or not all(isinstance(c, str) for c in choices):
            errors.append(f"params.{name} (type enum) requires a non-empty 'choices' array of strings")
            return None
        return ParamSpec(type="enum", default=default, choices=choices, description=description)

    if param_type == "string":
        pattern = spec.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or len(pattern) > _MAX_PATTERN_LENGTH:
                errors.append(f"params.{name}.pattern must be a string of at most {_MAX_PATTERN_LENGTH} characters")
                return None
            if _has_nested_quantifier(pattern):
                errors.append(f"params.{name}.pattern contains a nested quantifier: {pattern!r}")
                return None
            if not _is_safe_pattern(pattern):
                errors.append(f"params.{name}.pattern uses unsupported syntax: {pattern!r}")
                return None
            try:
                re.compile(pattern)
            except re.error as error:
                errors.append(f"params.{name}.pattern is not a valid regular expression: {error}")
                return None
        max_length = spec.get("max_length", _DEFAULT_MAX_LENGTH)
        if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length <= 0:
            errors.append(f"params.{name}.max_length must be a positive int")
            return None
        return ParamSpec(
            type="string", default=default, pattern=pattern, max_length=max_length, description=description
        )

    # int / float
    min_value = spec.get("min")
    if min_value is not None and not _is_finite_number(min_value):
        errors.append(f"params.{name}.min must be a finite number")
        return None
    max_value = spec.get("max")
    if max_value is not None and not _is_finite_number(max_value):
        errors.append(f"params.{name}.max must be a finite number")
        return None
    return ParamSpec(
        type=cast('Literal["int", "float"]', param_type),
        default=default,
        min=min_value,
        max=max_value,
        description=description,
    )


def _validate_placeholders(elements: list[str], location: str, params: dict[str, ParamSpec], errors: list[str]) -> None:
    for element in elements:
        for name in _PLACEHOLDER_RE.findall(element):
            if name in _RESERVED_PLACEHOLDERS:
                continue
            param = params.get(name) if _PARAM_NAME_PATTERN.fullmatch(name) else None
            if param is None:
                errors.append(f"{location} references undefined placeholder {{{name}}}")
                continue
            if param.type == "bool" and element != f"{{{name}}}":
                errors.append(
                    f"{location} element {element!r} mixes bool placeholder {{{name}}} with other text; "
                    "it must be its own element"
                )


def resolve_command(manifest: Manifest, values: dict[str, Any], *, host: str, app_dir: str) -> list[str]:
    """Render ``manifest.command`` with ``values``, filling declared defaults.

    Raises :class:`ManifestValidationError` if a required param is missing or
    a value fails its declared constraints (min/max/pattern/choices).
    """
    resolved = _resolve_params(manifest, values)
    result = []
    for element in manifest.command:
        substituted = _substitute(element, manifest.params, resolved, host, app_dir)
        if substituted is not None:
            result.append(substituted)
    return result


def resolve_url(manifest: Manifest, values: dict[str, Any], *, host: str, app_dir: str) -> str | None:
    """Render ``manifest.url`` with ``values``, or ``None`` if the manifest declares none."""
    if manifest.url is None:
        return None
    resolved = _resolve_params(manifest, values)
    return _substitute(manifest.url, manifest.params, resolved, host, app_dir)


def validate_param_values(manifest: Manifest, values: dict[str, Any]) -> None:
    """Validate ``values`` against their declared param specs, without requiring every param to be present.

    Used when persisting a partial set of param values (``PUT
    /apps/{id}/params``) -- completeness (every required param has a
    value) is instead checked at start time by :func:`resolve_command`.

    Raises:
        ManifestValidationError: an unknown param name, or a value failing
            its declared type/min/max/pattern/choices.
    """
    errors: list[str] = []
    for name, value in values.items():
        spec = manifest.params.get(name)
        if spec is None:
            errors.append(f"unknown param {name!r}")
            continue
        _validate_param_value(name, spec, value, errors)
    if errors:
        raise ManifestValidationError(errors)


def _resolve_params(manifest: Manifest, values: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    resolved: dict[str, Any] = {}
    for name, spec in manifest.params.items():
        if name in values:
            value = values[name]
            _validate_param_value(name, spec, value, errors)
        elif spec.has_default:
            value = spec.default
        else:
            errors.append(f"param {name!r} is required and has no value")
            continue
        resolved[name] = value
    if errors:
        raise ManifestValidationError(errors)
    return resolved


def _validate_param_value(name: str, spec: ParamSpec, value: Any, errors: list[str]) -> None:
    if spec.type == "bool":
        if not isinstance(value, bool):
            errors.append(f"param {name!r} must be a bool")
    elif spec.type == "enum":
        if value not in (spec.choices or []):
            errors.append(f"param {name!r} value {value!r} is not one of {spec.choices!r}")
    elif spec.type in ("int", "float"):
        expected = int if spec.type == "int" else (int, float)
        if not isinstance(value, expected) or isinstance(value, bool):
            errors.append(f"param {name!r} must be a {spec.type}")
            return
        if not math.isfinite(value):
            errors.append(f"param {name!r} must be a finite number")
            return
        if spec.min is not None and value < spec.min:
            errors.append(f"param {name!r} value {value!r} is below min {spec.min!r}")
        if spec.max is not None and value > spec.max:
            errors.append(f"param {name!r} value {value!r} is above max {spec.max!r}")
    elif spec.type == "string":
        if not isinstance(value, str):
            errors.append(f"param {name!r} must be a string")
            return
        if len(value) > spec.max_length:
            errors.append(f"param {name!r} exceeds max_length {spec.max_length}")
        if spec.pattern is not None and not re.fullmatch(spec.pattern, value):
            errors.append(f"param {name!r} value {value!r} does not match pattern {spec.pattern!r}")


def _substitute(
    element: str, params: dict[str, ParamSpec], values: dict[str, Any], host: str, app_dir: str
) -> str | None:
    names = _PLACEHOLDER_RE.findall(element)
    if not names:
        return element

    if len(names) == 1 and element == f"{{{names[0]}}}":
        sole_param = params.get(names[0])
        if sole_param is not None and sole_param.type == "bool":
            # A bool placeholder occupies the whole element: substitute its declared
            # flag when true, or drop the element entirely when false.
            return sole_param.flag if values[names[0]] else None

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "host":
            return host
        if name == "app_dir":
            return app_dir
        return str(values[name])

    return _PLACEHOLDER_RE.sub(replace, element)
