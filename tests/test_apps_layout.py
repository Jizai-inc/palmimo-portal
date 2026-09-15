"""Behavioral tests for `core/apps_layout.py`: the project/cwd/venv_python resolution rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_portal.core.apps_layout import resolve_layout
from palmimo_portal.ports import InvalidManifestSourceError


def test_resolve_layout_for_a_standalone_app_uses_its_own_root_as_project(tmp_path: Path) -> None:
    clone_root = tmp_path / "app"
    clone_root.mkdir()

    layout = resolve_layout(clone_root, None, name="app")

    assert layout.project == clone_root
    assert layout.cwd == clone_root
    assert layout.venv_python == clone_root / ".venv" / "bin" / "python"
    assert layout.package is None


def test_resolve_layout_for_a_standalone_subdir_app_has_matching_project_and_cwd(tmp_path: Path) -> None:
    clone_root = tmp_path / "repo"
    (clone_root / "examples" / "app").mkdir(parents=True)

    layout = resolve_layout(clone_root, "examples/app", name="app")

    assert layout.project == clone_root / "examples" / "app"
    assert layout.cwd == clone_root / "examples" / "app"
    assert layout.package is None


def test_resolve_layout_for_a_workspace_member_splits_project_from_cwd(tmp_path: Path) -> None:
    clone_root = tmp_path / "repo"
    member = clone_root / "examples" / "app"
    member.mkdir(parents=True)
    (clone_root / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['examples/*']\n", encoding="utf-8")
    (member / "pyproject.toml").write_text("[project]\nname = 'palmimo-app'\n", encoding="utf-8")

    layout = resolve_layout(clone_root, "examples/app", name="palmimo-app")

    assert layout.project == clone_root
    assert layout.cwd == clone_root / "examples" / "app"
    assert layout.venv_python == clone_root / ".venv" / "bin" / "python"
    assert layout.package == "palmimo-app"


def test_resolve_layout_for_a_workspace_member_uses_the_pyprojects_own_name_over_the_manifest_name(
    tmp_path: Path,
) -> None:
    # The manifest's declared app name and the member's pyproject.toml project name are not
    # guaranteed to match -- `uv sync --package` must be given the name uv itself resolves the
    # member by, not the app's own display name.
    clone_root = tmp_path / "repo"
    member = clone_root / "examples" / "app"
    member.mkdir(parents=True)
    (clone_root / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['examples/*']\n", encoding="utf-8")
    (member / "pyproject.toml").write_text("[project]\nname = 'actual-package-name'\n", encoding="utf-8")

    layout = resolve_layout(clone_root, "examples/app", name="palmimo-app")

    assert layout.package == "actual-package-name"


def test_resolve_layout_for_a_workspace_member_raises_when_pyproject_has_no_project_name(tmp_path: Path) -> None:
    clone_root = tmp_path / "repo"
    member = clone_root / "examples" / "app"
    member.mkdir(parents=True)
    (clone_root / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['examples/*']\n", encoding="utf-8")
    (member / "pyproject.toml").write_text("[build-system]\nrequires = []\n", encoding="utf-8")

    with pytest.raises(InvalidManifestSourceError):
        resolve_layout(clone_root, "examples/app", name="palmimo-app")


def test_resolve_layout_ignores_a_workspace_pyproject_that_does_not_parse(tmp_path: Path) -> None:
    clone_root = tmp_path / "repo"
    clone_root.mkdir()
    (clone_root / "pyproject.toml").write_text("not [ valid toml", encoding="utf-8")

    layout = resolve_layout(clone_root, None, name="app")

    assert layout.project == clone_root
    assert layout.package is None
