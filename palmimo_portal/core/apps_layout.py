"""Resolves where an app's ``uv`` project and working directory live (design doc 2.2b/3.3).

A uv-workspace member (the public devkit examples, cloned whole with
``subdir`` pointing at the member) shares one ``.venv`` at the clone root,
synced with ``--package <name>``; a standalone app (zip upload, or a git
source with no workspace) has its own ``.venv`` at its own root. Every
consumer that needs ``project``/``cwd``/the venv's ``python`` binary --
the dependency-sync job, the run-directory writer, the start precheck, and
startup finalize -- goes through :func:`resolve_layout` so the two shapes
never drift apart.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from palmimo_portal.core.apps import resolve_subdir
from palmimo_portal.ports import InvalidManifestSourceError


@dataclass(frozen=True)
class LayoutPaths:
    """One app's resolved ``uv`` project layout."""

    #: Directory passed to ``uv sync --project``/``UV_PROJECT_ENVIRONMENT`` -- where ``.venv`` lives.
    project: Path
    #: Directory the app's manifest, command, and process actually run from.
    cwd: Path
    venv_python: Path
    #: ``--package`` value for a uv-workspace member; ``None`` for a standalone project.
    package: str | None


def resolve_layout(clone_root: Path, subdir: str | None, *, name: str) -> LayoutPaths:
    """Resolve ``project``/``cwd``/``venv_python`` for an app rooted at ``clone_root``.

    ``name`` (the app's declared manifest name) is used only for error
    messages here -- the ``--package`` value passed to ``uv sync`` for a
    uv-workspace member is read straight from ``<cwd>/pyproject.toml``'s
    ``[project].name`` (the name ``uv``'s own resolver actually keys the
    member on), not assumed to match the manifest's.

    Raises:
        InvalidManifestSourceError: ``clone_root`` is a workspace root but
            ``<cwd>/pyproject.toml`` is missing, unparseable, or has no
            ``[project].name``.
    """
    cwd = resolve_subdir(clone_root, subdir)
    if _is_workspace_root(clone_root):
        project, package = clone_root, _workspace_package_name(cwd, name)
    else:
        project, package = cwd, None
    return LayoutPaths(project=project, cwd=cwd, venv_python=project / ".venv" / "bin" / "python", package=package)


def _workspace_package_name(cwd: Path, app_name: str) -> str:
    pyproject = cwd / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as error:
        raise InvalidManifestSourceError(
            f"app {app_name!r}: cannot read {pyproject} to resolve its workspace package name: {error}"
        ) from error
    package_name = data.get("project", {}).get("name")
    if not isinstance(package_name, str) or not package_name:
        raise InvalidManifestSourceError(f"app {app_name!r}: {pyproject} has no [project].name")
    return package_name


def _is_workspace_root(clone_root: Path) -> bool:
    pyproject = clone_root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return False
    return "workspace" in data.get("tool", {}).get("uv", {})
