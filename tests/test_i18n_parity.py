"""i18n parity: the frontend's locale resources must stay in lockstep with the backend.

Two things can drift silently otherwise: `en.json` and `ja.json` growing
different key sets (a translator adds a key to one and forgets the other), and
a backend error code (`PortalError`'s `code`, per errors.py's module
docstring) that has no `errors.<code>` entry for either locale to resolve --
the frontend would then fall back to a raw code string in the UI (see
src/components/ApiErrorAlert.tsx's `defaultValue`).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_I18N_DIR = PACKAGE_ROOT / "frontend" / "src" / "i18n"
FRONTEND_SRC_DIR = PACKAGE_ROOT / "frontend" / "src"
BACKEND_SOURCE_DIR = PACKAGE_ROOT / "palmimo_portal"

# `t("dotted.key")` / `t('dotted.key')` -- react-i18next's translate function,
# called with a literal key. The negative lookbehind keeps this from matching
# any other identifier that merely ends in "t(" (e.g. `headers.set("Accept",
# ...)`) rather than the `const { t } = useTranslation()` binding every route
# and component uses. A *non*-literal key -- `t(\`errors.${error.code}\`, ...)`
# in components/ApiErrorAlert.tsx -- does not match at all, which is
# deliberate: that call resolves a backend-supplied error code at runtime, not
# a key this static scan could ever enumerate (see the errors.* carve-out in
# test_every_locale_key_is_used_somewhere below).
T_CALL_KEY_PATTERN = re.compile(r"(?<![\w$])t\(\s*[\"']([A-Za-z0-9_.]+)[\"']")

# `{{paramName}}` -- i18next's interpolation syntax, used throughout
# src/i18n/en.json / ja.json (e.g. `"{{ssid}}"`, `"{{retry_after_seconds}}"`).
INTERPOLATION_PARAM_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# The two call targets this backend hands a machine-readable error code to,
# and which positional/keyword argument carries it. `code` is always the
# second parameter (`PortalError(status_code, code, **params)`,
# `_error_envelope(status_code, code, params)`), so a call site can spell it
# either positionally (`PortalError(409, "auth_not_set")`) or as a keyword
# (`PortalError(409, code="auth_not_set")` or, with every argument named,
# `PortalError(status_code=409, code="auth_not_set")`) -- an AST walk checks
# both forms rather than a source-text regex, which only ever matched the
# positional spelling and would silently stop counting a code the moment a
# call site (or a future one) used the keyword form.
_CODE_ARG_CALLEES = frozenset({"PortalError", "_error_envelope"})


def _string_literal(node: ast.expr | None) -> str | None:
    """Return `node`'s string value if it is a literal, else `None`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _codes_from_call(call: ast.Call) -> set[str]:
    if not (isinstance(call.func, ast.Name) and call.func.id in _CODE_ARG_CALLEES):
        return set()
    code = _string_literal(call.args[1]) if len(call.args) >= 2 else None
    if code is None:
        code = next(
            (_string_literal(kw.value) for kw in call.keywords if kw.arg == "code" and _string_literal(kw.value)),
            None,
        )
    return {code} if code is not None else set()


def _codes_from_dict(node: ast.Dict) -> set[str]:
    # The bare `{"code": "...", "params": ...}` envelopes HostGuard/CSRF
    # (app.py's middleware) and the SPA fallback's 404s build directly,
    # bypassing both PortalError and _error_envelope.
    codes: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "code":
            literal = _string_literal(value)
            if literal is not None:
                codes.add(literal)
    return codes


def _codes_from_assign(node: ast.Assign) -> set[str]:
    # `_ADAPTER_ERROR_CODE = "..."` -- the constant the real adapters
    # (comitup.py, systemd.py) raise AdapterUnavailableError with, one
    # assignment away from a string literal PortalError(...) would use
    # inline.
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
        return set()
    if node.targets[0].id != "_ADAPTER_ERROR_CODE":
        return set()
    literal = _string_literal(node.value)
    return {literal} if literal is not None else set()


def _flatten_keys(node: object, prefix: str = "") -> set[str]:
    """Return every dotted leaf-key path in a nested JSON object."""
    if not isinstance(node, dict):
        return {prefix}
    keys: set[str] = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        keys |= _flatten_keys(value, path)
    return keys


def _load_locale(name: str) -> dict:
    with (FRONTEND_I18N_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _leaf_strings(node: object, prefix: str = "") -> dict[str, str]:
    """Return every dotted leaf-key path in a nested JSON object, mapped to its string value."""
    if not isinstance(node, dict):
        return {prefix: node} if isinstance(node, str) else {}
    values: dict[str, str] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        values.update(_leaf_strings(value, path))
    return values


def _translation_keys_used_in_frontend() -> set[str]:
    """Every literal key some `t("...")` call in frontend/src passes react-i18next.

    Skips `src/api/generated/` -- generated orval output, not hand-written UI
    code, and no more a place a translation key would appear than
    palmimo_portal itself is.
    """
    used: set[str] = set()
    for path in FRONTEND_SRC_DIR.rglob("*.ts*"):
        if "generated" in path.relative_to(FRONTEND_SRC_DIR).parts:
            continue
        used |= set(T_CALL_KEY_PATTERN.findall(path.read_text(encoding="utf-8")))
    return used


def _backend_error_codes() -> set[str]:
    """Every error code this backend's Python source can hand to a response."""
    codes: set[str] = set()
    for path in BACKEND_SOURCE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                codes |= _codes_from_call(node)
            elif isinstance(node, ast.Dict):
                codes |= _codes_from_dict(node)
            elif isinstance(node, ast.Assign):
                codes |= _codes_from_assign(node)
    return codes


def test_locale_files_have_identical_key_sets() -> None:
    en_keys = _flatten_keys(_load_locale("en.json"))
    ja_keys = _flatten_keys(_load_locale("ja.json"))

    assert en_keys == ja_keys, (
        f"en.json and ja.json must declare the same keys. "
        f"Only in en.json: {sorted(en_keys - ja_keys)}. Only in ja.json: {sorted(ja_keys - en_keys)}."
    )


def test_every_backend_error_code_has_an_en_translation() -> None:
    codes = _backend_error_codes()
    en = _load_locale("en.json")["errors"]

    missing = sorted(code for code in codes if code not in en)
    assert missing == [], f"src/i18n/en.json's errors table is missing: {missing}"


def test_every_backend_error_code_has_a_ja_translation() -> None:
    codes = _backend_error_codes()
    ja = _load_locale("ja.json")["errors"]

    missing = sorted(code for code in codes if code not in ja)
    assert missing == [], f"src/i18n/ja.json's errors table is missing: {missing}"


def test_the_error_code_scan_finds_the_codes_this_test_was_written_for() -> None:
    # A regression guard on the scanner itself: if a refactor changes how
    # codes are raised badly enough that the AST walk stops matching
    # anything, the two tests above would pass vacuously instead of failing
    # loudly.
    codes = _backend_error_codes()
    for expected in ("not_authenticated", "wifi_connect_failed", "host_not_allowed", "network_backend_unavailable"):
        assert expected in codes, (
            f"the error-code scan no longer finds {expected!r} -- update _codes_from_call/_codes_from_dict/_codes_from_assign"
        )


def test_the_error_code_scan_finds_a_code_passed_as_a_keyword_argument() -> None:
    # PortalError's every current call site spells `code` positionally
    # (`PortalError(409, "auth_not_set")`), so a scan that only understood
    # that form would pass today and still miss a code the moment a call
    # site -- or a future one -- spelled it as a keyword instead
    # (`PortalError(409, code="auth_not_set")`, or with every argument
    # named). This exercises that form directly, against a source snippet
    # this test owns, so it fails if the AST walk ever regresses to
    # positional-only.
    source = (
        "PortalError(409, code='kw_only_code')\n"
        "PortalError(status_code=409, code='kw_only_code_named_status')\n"
        "_error_envelope(500, code='kw_only_envelope_code', params={})\n"
    )
    tree = ast.parse(source)
    codes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            codes |= _codes_from_call(node)

    assert codes == {"kw_only_code", "kw_only_code_named_status", "kw_only_envelope_code"}


def test_no_translation_table_entry_is_orphaned() -> None:
    # The reverse direction: an errors.<code> key with no backend code left
    # raising it is dead translation debt, not a missing one.
    codes = _backend_error_codes()
    en_errors = set(_load_locale("en.json")["errors"])

    orphaned = sorted(en_errors - codes)
    assert orphaned == [], f"src/i18n/en.json's errors table has entries no backend code raises: {orphaned}"


def test_every_translation_call_key_exists_in_both_locales() -> None:
    used = _translation_keys_used_in_frontend()
    en_keys = _flatten_keys(_load_locale("en.json"))
    ja_keys = _flatten_keys(_load_locale("ja.json"))

    missing_en = sorted(key for key in used if key not in en_keys)
    missing_ja = sorted(key for key in used if key not in ja_keys)
    assert missing_en == [], f"frontend/src calls t(...) with keys missing from en.json: {missing_en}"
    assert missing_ja == [], f"frontend/src calls t(...) with keys missing from ja.json: {missing_ja}"


def test_every_locale_key_is_used_somewhere() -> None:
    # The reverse direction: a key nothing in frontend/src references any
    # more is dead translation debt -- delete it (or wire it up) rather than
    # carrying it. `errors.<code>` keys are exempt: they are resolved
    # dynamically from a backend-supplied code
    # (components/ApiErrorAlert.tsx's `t(\`errors.${error.code}\`, ...)`),
    # not through a literal `t("errors....")` call this static scan can see
    # -- test_every_backend_error_code_has_an_en_translation and
    # test_no_translation_table_entry_is_orphaned already hold that table to
    # account, from the backend-code side.
    used = _translation_keys_used_in_frontend()
    en_keys = _flatten_keys(_load_locale("en.json"))
    non_error_keys = {key for key in en_keys if not key.startswith("errors.")}

    unused = sorted(key for key in non_error_keys if key not in used)
    assert unused == [], f"src/i18n/en.json declares keys nothing in frontend/src uses: {unused}"


def test_interpolation_params_match_between_locales() -> None:
    # A `{{param}}` en.json interpolates that ja.json's translation of the
    # same key does not (or vice versa) is a bug the render only surfaces as
    # a literal "{{param}}" left in the rendered UI -- worth catching here
    # rather than by eyeballing a translation diff.
    en_leaves = _leaf_strings(_load_locale("en.json"))
    ja_leaves = _leaf_strings(_load_locale("ja.json"))

    mismatches: dict[str, dict[str, list[str]]] = {}
    for key in sorted(en_leaves.keys() & ja_leaves.keys()):
        en_params = set(INTERPOLATION_PARAM_PATTERN.findall(en_leaves[key]))
        ja_params = set(INTERPOLATION_PARAM_PATTERN.findall(ja_leaves[key]))
        if en_params != ja_params:
            mismatches[key] = {"en": sorted(en_params), "ja": sorted(ja_params)}

    assert mismatches == {}, f"interpolation params differ between en.json and ja.json: {mismatches}"
