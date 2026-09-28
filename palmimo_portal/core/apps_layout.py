"""Resolves where an app's ``uv`` project and working directory live (design doc 2.2b/3.3).

An app's directory (the clone root, or ``subdir`` inside it) is always the
``uv`` project and the ``.venv`` location; whether that directory is also a
member of a workspace rooted above it is left entirely to ``uv`` itself to
discover at sync time (via ``UV_PROJECT_ENVIRONMENT`` and plain ``uv sync``,
no ``--package``) -- the Portal does not reimplement uv's workspace-membership
rules. Every consumer that needs ``project``/``cwd``/the venv's ``python``
binary -- the dependency-sync job, the run-directory writer, the start
precheck, and startup finalize -- goes through :func:`resolve_layout` so this
stays the one place that decision is made.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from palmimo_portal.core.apps import resolve_subdir


@dataclass(frozen=True)
class LayoutPaths:
    """One app's resolved ``uv`` project layout."""

    #: Directory passed to ``uv sync --project``/``UV_PROJECT_ENVIRONMENT`` -- where ``.venv`` lives.
    project: Path
    #: Directory the app's manifest, command, and process actually run from. Always equal to ``project``.
    cwd: Path
    venv_python: Path


def resolve_layout(clone_root: Path, subdir: str | None) -> LayoutPaths:
    """Resolve ``project``/``cwd``/``venv_python`` for an app rooted at ``clone_root``.

    Raises:
        InvalidManifestSourceError: ``subdir`` escapes ``clone_root`` (see
            :func:`~palmimo_portal.core.apps.resolve_subdir`).
    """
    cwd = resolve_subdir(clone_root, subdir)
    return LayoutPaths(project=cwd, cwd=cwd, venv_python=cwd / ".venv" / "bin" / "python")
