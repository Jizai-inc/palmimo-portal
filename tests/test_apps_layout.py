"""Behavioral tests for `core/apps_layout.py`: the project/cwd/venv_python resolution rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_portal.core.apps_layout import resolve_layout
from palmimo_portal.ports import InvalidManifestSourceError


def test_resolve_layout_for_a_standalone_app_uses_its_own_root_as_project(tmp_path: Path) -> None:
    clone_root = tmp_path / "app"
    clone_root.mkdir()

    layout = resolve_layout(clone_root, None)

    assert layout.project == clone_root
    assert layout.cwd == clone_root
    assert layout.venv_python == clone_root / ".venv" / "bin" / "python"


def test_resolve_layout_for_a_subdir_app_has_matching_project_and_cwd(tmp_path: Path) -> None:
    clone_root = tmp_path / "repo"
    (clone_root / "examples" / "app").mkdir(parents=True)

    layout = resolve_layout(clone_root, "examples/app")

    assert layout.project == clone_root / "examples" / "app"
    assert layout.cwd == clone_root / "examples" / "app"


def test_resolve_layout_for_a_subdir_under_a_workspace_root_not_listing_it_uses_the_subdir_as_project(
    tmp_path: Path,
) -> None:
    # The devkit shape: `apps/<name>/` clones the whole devkit repo, but the app's own
    # `subdir` (an example) is not one of the workspace's declared `members`. Membership
    # is uv's decision at sync time, not this function's -- the resolved layout does not
    # change either way.
    clone_root = tmp_path / "repo"
    (clone_root / "examples" / "app").mkdir(parents=True)
    (clone_root / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['packages/*']\n", encoding="utf-8")

    layout = resolve_layout(clone_root, "examples/app")

    assert layout.project == clone_root / "examples" / "app"
    assert layout.cwd == clone_root / "examples" / "app"


def test_resolve_layout_for_a_subdir_that_is_a_workspace_member_still_uses_the_subdir_as_project(
    tmp_path: Path,
) -> None:
    clone_root = tmp_path / "repo"
    (clone_root / "examples" / "app").mkdir(parents=True)
    (clone_root / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['examples/*']\n", encoding="utf-8")

    layout = resolve_layout(clone_root, "examples/app")

    assert layout.project == clone_root / "examples" / "app"
    assert layout.cwd == clone_root / "examples" / "app"


def test_resolve_layout_rejects_a_subdir_escaping_the_clone_root(tmp_path: Path) -> None:
    clone_root = tmp_path / "repo"
    clone_root.mkdir()

    with pytest.raises(InvalidManifestSourceError):
        resolve_layout(clone_root, "../outside")
