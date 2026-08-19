"""AST-based import-discipline contracts for :mod:`palmimo_portal`.

These are deterministic, mechanically-checkable rules about *which module
may import which* -- the kind of thing a docstring can assert but only a
test can actually hold the line on as the tree grows. Each check parses the
source with :mod:`ast` rather than grepping text, so a string that merely
*mentions* a module name (a docstring, a comment) never produces a false
positive or negative.

The layering these contracts encode (see ``palmimo_portal/wiring.py``'s and
``palmimo_portal/api/app.py``'s module docstrings for the narrative version):

- ``api/`` is the only place ``fastapi``/``starlette`` may be imported
  directly (``uvicorn`` in ``__main__.py`` is a separate exception, and is
  not ``fastapi``/``starlette`` either way).
- ``core/`` depends on stdlib and :mod:`palmimo_portal.ports` only -- never
  ``adapters/``, ``api/``, or ``fastapi``/``starlette``. It is the
  OS-independent, HTTP-independent use-case layer.
- ``api/`` never imports ``adapters/`` directly -- only
  :mod:`palmimo_portal.wiring` and :mod:`palmimo_portal.testing` build
  concrete adapters; ``api/`` only ever sees them through the
  :class:`~palmimo_portal.wiring.AdapterBundle` on ``request.app.state``.
- :mod:`palmimo_portal.testing.fakes` (an in-memory fake implementation of
  every port) never imports from ``adapters/`` (the real, OS-backed
  implementation) -- the two are independent implementations of the same
  ports, and a fake depending on a real adapter would be backwards.
- :mod:`palmimo_portal.adapters.comitup` only ever talks to comitup's own
  D-Bus interface -- never NetworkManager's ``org.freedesktop.NetworkManager``
  D-Bus interface, never shells out to ``nmcli``, and never imports
  :mod:`subprocess` at all (the only way this adapter could shell out to
  anything). comitup owns NetworkManager on this device; anything reaching
  around it would race comitup's own state machine.

Every "which module does this import" check below considers three ways a
file could reach a banned target, not just the most obvious one:

1. A plain ``import x`` / ``from x import y`` -- the common case.
2. A **relative** import (``from . import adapters``, ``from ..adapters
   import x``) -- resolved to its absolute dotted name using the
   *importing file's own package*, so it is checked exactly like an
   absolute import instead of being silently skipped (this tree happens
   not to use relative imports today, but nothing stops a future edit from
   introducing one, and an unresolved relative import would otherwise slip
   straight through every check below).
3. A **dynamic** import -- ``importlib.import_module("...")`` or
   ``__import__("...")`` with a string-literal argument -- which reaches
   the same target at runtime without ever writing a static ``import``
   statement an AST-only literal-import scan would notice.
4. ``from palmimo_portal import adapters`` (and any alias of it) --
   distinct from ``import palmimo_portal.adapters``: the module named on
   the ``from`` clause is ``palmimo_portal`` itself, so the submodule name
   only appears in the *names* being imported, not in ``node.module``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).parent.parent / "palmimo_portal"


def _iter_py_files(*, exclude: frozenset[str] = frozenset()) -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and path.relative_to(PACKAGE_ROOT).as_posix() not in exclude
    )


# -- relative-import resolution ----------------------------------------------------


def _module_dotted_name(path: Path) -> str:
    """Return *path*'s own dotted module name (``palmimo_portal/core/auth.py`` -> ``"palmimo_portal.core.auth"``)."""
    relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_dotted_name(path: Path) -> str:
    """Return the dotted package *path* itself belongs to (what ``__package__`` resolves to at runtime)."""
    module = _module_dotted_name(path)
    if path.name == "__init__.py":
        return module
    parts = module.split(".")
    return ".".join(parts[:-1])


def _resolve_relative_import(path: Path, node: ast.ImportFrom) -> str | None:
    """Resolve one relative ``ast.ImportFrom`` (``node.level > 0``) to its absolute dotted target, if any.

    ``node.level == 1`` ("``from . import x``" / "``from .y import x``")
    means "relative to this file's own package"; each additional level
    climbs one more parent package, matching Python's own relative-import
    resolution rule. Returns ``None`` only if the import climbs above the
    package root -- not a shape this tree ever produces, and not something
    worth crashing a contract test over.
    """
    package_parts = _package_dotted_name(path).split(".") if _package_dotted_name(path) else []
    climb = node.level - 1
    if climb > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - climb] if climb else list(package_parts)
    if node.module:
        base_parts = base_parts + node.module.split(".")
    return ".".join(base_parts) if base_parts else None


# -- dynamic-import detection -------------------------------------------------------


def _dynamic_import_literal_targets(path: Path) -> set[str]:
    """Return every string-literal argument to an ``importlib.import_module()``/``__import__()`` call in *path*.

    Matches only an actual call to one of the two stdlib dynamic-import
    entry points, with a literal string first argument -- a computed or
    f-string argument is not something this can resolve statically, and
    is not a pattern this codebase uses to reach a banned module.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_import_module = (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        )
        is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
        if not (is_import_module or is_dunder_import):
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            targets.add(first_arg.value)
    return targets


def _dynamic_top_level_modules(path: Path) -> set[str]:
    return {target.split(".")[0] for target in _dynamic_import_literal_targets(path)}


def _dynamic_palmimo_portal_submodules(path: Path) -> set[str]:
    modules: set[str] = set()
    for target in _dynamic_import_literal_targets(path):
        if target == "palmimo_portal" or target.startswith("palmimo_portal."):
            parts = target.split(".")
            modules.add(".".join(parts[:2]) if len(parts) > 1 else parts[0])
    return modules


# -- static-import resolution --------------------------------------------------------


def _imported_top_level_modules(path: Path) -> set[str]:
    """Return every top-level dotted module name *path* imports (``import x.y`` -> ``{"x"}``, etc.).

    Includes relative imports, resolved via :func:`_resolve_relative_import` --
    a third-party package (the only thing this particular check cares
    about) is never reached through one in practice, but resolving them
    anyway keeps this function's contract simple: every import, absolute or
    relative, ends up in the same set.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module is not None:
                    modules.add(node.module.split(".")[0])
            else:
                resolved = _resolve_relative_import(path, node)
                if resolved:
                    modules.add(resolved.split(".")[0])
    return modules


def _imported_palmimo_portal_submodules(path: Path) -> set[str]:
    """Return every ``palmimo_portal.<x>`` dotted prefix *path* imports (e.g. ``"palmimo_portal.adapters"``).

    Covers all four shapes the module docstring lists: absolute
    (``import palmimo_portal.adapters.comitup``), relative
    (``from ..adapters import x`` inside ``core/``), the
    ``from palmimo_portal import adapters`` form (the submodule name lives
    in ``node.names``, not ``node.module``), and its relative analogue
    (``from . import adapters``).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()

    def add_dotted(dotted: str) -> None:
        if dotted == "palmimo_portal" or dotted.startswith("palmimo_portal."):
            parts = dotted.split(".")
            modules.add(".".join(parts[:2]) if len(parts) > 1 else parts[0])

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module is not None:
                    add_dotted(node.module)
                    if node.module == "palmimo_portal":
                        for alias in node.names:
                            add_dotted(f"palmimo_portal.{alias.name}")
            else:
                resolved = _resolve_relative_import(path, node)
                if resolved is None:
                    continue
                add_dotted(resolved)
                if node.module is None:
                    # "from . import adapters" / "from .. import adapters":
                    # the submodule name lives in the imported names, not on
                    # the (empty) module clause -- resolved is just the
                    # containing package here.
                    for alias in node.names:
                        add_dotted(f"{resolved}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                add_dotted(alias.name)
    return modules


_WEB_FRAMEWORK_MODULES = frozenset({"fastapi", "starlette"})


@pytest.mark.parametrize("path", _iter_py_files(), ids=lambda p: p.relative_to(PACKAGE_ROOT).as_posix())
def test_only_api_imports_fastapi_or_starlette(path: Path) -> None:
    relative = path.relative_to(PACKAGE_ROOT).as_posix()
    if relative.startswith("api/"):
        pytest.skip("api/ is the one place fastapi/starlette imports are allowed")
    imported = _imported_top_level_modules(path) | _dynamic_top_level_modules(path)
    offending = imported & _WEB_FRAMEWORK_MODULES
    assert not offending, (
        f"{relative} imports {sorted(offending)} directly -- only palmimo_portal/api/ may import "
        f"fastapi/starlette (uvicorn in __main__.py is a separate, unrelated exception)"
    )


_CORE_FORBIDDEN_PREFIXES = frozenset(
    {"palmimo_portal.adapters", "palmimo_portal.api", "palmimo_portal.testing", "palmimo_portal.wiring"}
)


@pytest.mark.parametrize("path", _iter_py_files(), ids=lambda p: p.relative_to(PACKAGE_ROOT).as_posix())
def test_core_never_imports_adapters_api_or_web_frameworks(path: Path) -> None:
    relative = path.relative_to(PACKAGE_ROOT).as_posix()
    if not relative.startswith("core/"):
        pytest.skip("only core/ is constrained by this rule")

    imported_top_level = _imported_top_level_modules(path) | _dynamic_top_level_modules(path)
    offending_frameworks = imported_top_level & _WEB_FRAMEWORK_MODULES
    assert not offending_frameworks, f"{relative} (in core/) imports {sorted(offending_frameworks)} directly"

    imported_submodules = _imported_palmimo_portal_submodules(path) | _dynamic_palmimo_portal_submodules(path)
    offending_layers = imported_submodules & _CORE_FORBIDDEN_PREFIXES
    assert not offending_layers, (
        f"{relative} (in core/) imports {sorted(offending_layers)} -- core/ may depend only on stdlib, "
        f"third-party pure-logic libraries, and palmimo_portal.ports"
    )


@pytest.mark.parametrize("path", _iter_py_files(), ids=lambda p: p.relative_to(PACKAGE_ROOT).as_posix())
def test_api_never_imports_adapters_directly(path: Path) -> None:
    relative = path.relative_to(PACKAGE_ROOT).as_posix()
    if not relative.startswith("api/"):
        pytest.skip("only api/ is constrained by this rule")

    imported_submodules = _imported_palmimo_portal_submodules(path) | _dynamic_palmimo_portal_submodules(path)
    assert "palmimo_portal.adapters" not in imported_submodules, (
        f"{relative} (in api/) imports palmimo_portal.adapters directly -- only "
        f"palmimo_portal.wiring and palmimo_portal.testing may construct concrete adapters"
    )


def test_testing_fakes_does_not_import_from_adapters() -> None:
    path = PACKAGE_ROOT / "testing" / "fakes.py"

    imported_submodules = _imported_palmimo_portal_submodules(path) | _dynamic_palmimo_portal_submodules(path)

    assert "palmimo_portal.adapters" not in imported_submodules, (
        "testing/fakes.py imports from palmimo_portal.adapters -- a fake depending on a real "
        "adapter is backwards; move any shared pure logic to core/ instead (see core/sshkey.py)"
    )


def test_comitup_adapter_never_reaches_around_comitup_to_networkmanager_or_nmcli() -> None:
    source = (PACKAGE_ROOT / "adapters" / "comitup.py").read_text(encoding="utf-8")

    assert "org.freedesktop.NetworkManager" not in source, (
        "adapters/comitup.py must talk to comitup's own D-Bus interface only -- touching "
        "NetworkManager's interface directly would race comitup's own state machine"
    )
    assert "nmcli" not in source, "adapters/comitup.py must not shell out to nmcli -- comitup owns NetworkManager"


def test_comitup_adapter_never_imports_subprocess() -> None:
    path = PACKAGE_ROOT / "adapters" / "comitup.py"

    imported = _imported_top_level_modules(path) | _dynamic_top_level_modules(path)

    assert "subprocess" not in imported, (
        "adapters/comitup.py must not import subprocess at all -- shelling out to nmcli (or anything "
        "else) would race comitup's own state machine; talk to comitup's own D-Bus interface only. "
        "Banning the import itself (not just the literal string 'nmcli') closes the "
        "'assemble the command from parts' evasion of the nmcli text check above."
    )


# -- planted-violation tests: prove each detection actually detects -----------------


def test_relative_import_of_adapters_from_core_is_detected(tmp_path: Path) -> None:
    # A synthetic file at palmimo_portal/core/auth.py's own path shape, so
    # _resolve_relative_import has a real package to resolve against.
    package_root = tmp_path / "palmimo_portal"
    core_dir = package_root / "core"
    core_dir.mkdir(parents=True)
    source = core_dir / "auth.py"
    source.write_text("from ..adapters import comitup\n", encoding="utf-8")

    global PACKAGE_ROOT
    original_root = PACKAGE_ROOT
    PACKAGE_ROOT = package_root
    try:
        submodules = _imported_palmimo_portal_submodules(source)
    finally:
        PACKAGE_ROOT = original_root

    assert "palmimo_portal.adapters" in submodules


def test_relative_import_of_a_sibling_module_within_core_is_not_flagged(tmp_path: Path) -> None:
    package_root = tmp_path / "palmimo_portal"
    core_dir = package_root / "core"
    core_dir.mkdir(parents=True)
    source = core_dir / "auth.py"
    source.write_text("from .provisioning import is_provisioned\n", encoding="utf-8")

    global PACKAGE_ROOT
    original_root = PACKAGE_ROOT
    PACKAGE_ROOT = package_root
    try:
        submodules = _imported_palmimo_portal_submodules(source)
    finally:
        PACKAGE_ROOT = original_root

    assert "palmimo_portal.adapters" not in submodules
    assert "palmimo_portal.core" in submodules


def test_from_palmimo_portal_import_adapters_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("from palmimo_portal import adapters\n", encoding="utf-8")

    assert "palmimo_portal.adapters" in _imported_palmimo_portal_submodules(source)


def test_from_palmimo_portal_import_adapters_as_alias_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("from palmimo_portal import adapters as ad\n", encoding="utf-8")

    assert "palmimo_portal.adapters" in _imported_palmimo_portal_submodules(source)


def test_dynamic_import_module_of_a_banned_target_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text(
        'import importlib\nimportlib.import_module("palmimo_portal.adapters.comitup")\n', encoding="utf-8"
    )

    assert "palmimo_portal.adapters" in _dynamic_palmimo_portal_submodules(source)


def test_dunder_import_of_a_banned_target_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text('__import__("fastapi")\n', encoding="utf-8")

    assert "fastapi" in _dynamic_top_level_modules(source)


def test_dynamic_import_with_a_computed_argument_is_not_flagged(tmp_path: Path) -> None:
    # Not a pattern this codebase uses, and not statically resolvable --
    # documented here as the known, deliberate limit of this detection.
    source = tmp_path / "module.py"
    source.write_text('import importlib\nname = "fastapi"\nimportlib.import_module(name)\n', encoding="utf-8")

    assert _dynamic_top_level_modules(source) == set()


def test_import_subprocess_is_detected_as_a_top_level_import(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("import subprocess\n", encoding="utf-8")

    assert "subprocess" in _imported_top_level_modules(source)
