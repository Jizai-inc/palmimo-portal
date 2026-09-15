"""Integration test: the real app-sync helper produces a working relocatable venv (design doc 2.2b).

Skipped when ``uv`` is not on ``PATH``, or when the helper script cannot be
found. No fake proves this: the design stages a ``.venv`` under
``.staging/<id>/`` and moves the whole directory into its final home with a
bare ``rename`` -- without a real ``uv venv --relocatable`` + ``uv sync``
run followed by an actual rename, a broken (non-relocatable) console-script
shebang would ship silently.

Marked ``integration`` (registered in ``pyproject.toml``, excluded from the
default run by ``addopts = "-m 'not integration'"``) because it shells out
to a real ``uv`` and takes tens of seconds -- run it explicitly with
``uv run pytest -m integration tests/test_apps_sync_integration.py``.

The helper script's location: set ``PALMIMO_APP_SYNC_HELPER`` to an
absolute path, or check out the ``palmimo-image`` repository's
``platform-bundle`` branch/worktree as a sibling of this monorepo's root
(``../infra/image`` from this repository's own root) -- the fallback below
assumes that layout, matching how a maintainer's machine is normally set up
per the root ``AGENTS.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _default_helper_script() -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    return (
        repo_root.parent
        / "infra"
        / "image"
        / ".claude"
        / "worktrees"
        / "platform-bundle"
        / "platform"
        / "files"
        / "usr"
        / "lib"
        / "palmimo"
        / "app-sync"
    )


HELPER_SCRIPT = (
    Path(os.environ["PALMIMO_APP_SYNC_HELPER"]) if "PALMIMO_APP_SYNC_HELPER" in os.environ else _default_helper_script()
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH"),
    pytest.mark.skipif(
        not HELPER_SCRIPT.is_file(),
        reason=(
            "the app-sync helper was not found -- set PALMIMO_APP_SYNC_HELPER "
            f"or check out palmimo-image's platform-bundle worktree (looked at {HELPER_SCRIPT})"
        ),
    ),
]

_STANDALONE_PYPROJECT = """\
[project]
name = "sync-integration-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
sync-integration-fixture = "sync_integration_fixture:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["sync_integration_fixture"]
"""

_WORKSPACE_ROOT_PYPROJECT = """\
[tool.uv.workspace]
members = ["member"]
"""

_WORKSPACE_MEMBER_PYPROJECT = """\
[project]
name = "sync-integration-member"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
sync-integration-fixture = "sync_integration_fixture:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["sync_integration_fixture"]
"""

_MODULE_SOURCE = "def main() -> None:\n    print('ok')\n"


def _write_package(root: Path) -> None:
    package_dir = root / "sync_integration_fixture"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(_MODULE_SOURCE, encoding="utf-8")


def _seed_standalone(staged_project: Path, *, frozen: bool) -> None:
    staged_project.mkdir(parents=True)
    (staged_project / "pyproject.toml").write_text(_STANDALONE_PYPROJECT, encoding="utf-8")
    _write_package(staged_project)
    if frozen:
        # A pre-generated lock exercises the `--frozen` path; omitting this exercises the
        # helper's own `uv lock` fallback (`frozen=False`) -- both are real device inputs
        # (whether a job's source already carries `uv.lock`).
        result = subprocess.run(
            ["uv", "lock", "--project", str(staged_project)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr


def _seed_workspace(staged_project: Path, *, frozen: bool) -> Path:
    """Seed a uv-workspace root with one member and return the member's project dir."""
    staged_project.mkdir(parents=True)
    (staged_project / "pyproject.toml").write_text(_WORKSPACE_ROOT_PYPROJECT, encoding="utf-8")
    member = staged_project / "member"
    member.mkdir()
    (member / "pyproject.toml").write_text(_WORKSPACE_MEMBER_PYPROJECT, encoding="utf-8")
    _write_package(member)
    if frozen:
        result = subprocess.run(
            ["uv", "lock", "--project", str(staged_project)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
    return member


#: Generous: a cold `uv`/pip cache has to download and build an isolated
#: environment for the hatchling build backend before it can build this
#: fixture's own wheel -- observed to take several minutes on a first run.
_HELPER_TIMEOUT_SECONDS = 600


def _run_helper(staging_root: Path, instance: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PALMIMO_STAGING_DIR": str(staging_root)}
    return subprocess.run(
        [sys.executable, str(HELPER_SCRIPT), instance],
        env=env,
        capture_output=True,
        text=True,
        timeout=_HELPER_TIMEOUT_SECONDS,
    )


def _assert_console_script_runs(venv_dir: Path) -> None:
    console_script = venv_dir / "bin" / "sync-integration-fixture"
    assert console_script.is_file()
    run = subprocess.run([str(console_script)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0
    assert run.stdout.strip() == "ok"


@pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "unfrozen"])
def test_standalone_project_console_script_survives_a_directory_rename(tmp_path: Path, frozen: bool) -> None:
    staging_root = tmp_path / "staging"
    instance = "j" + "0" * 31
    staged_project = staging_root / instance
    _seed_standalone(staged_project, frozen=frozen)
    (staged_project / "sync.json").write_text(
        json.dumps({"project": str(staged_project), "package": None, "frozen": frozen, "relocatable": True}),
        encoding="utf-8",
    )

    result = _run_helper(staging_root, instance)
    assert result.returncode == 0, result.stderr

    final_project = tmp_path / "final"
    staged_project.rename(final_project)

    _assert_console_script_runs(final_project / ".venv")


def test_workspace_member_console_script_survives_a_directory_rename_when_synced_with_package(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    instance = "j" + "1" * 31
    staged_project = staging_root / instance
    _seed_workspace(staged_project, frozen=True)
    (staged_project / "sync.json").write_text(
        json.dumps(
            {"project": str(staged_project), "package": "sync-integration-member", "frozen": True, "relocatable": True}
        ),
        encoding="utf-8",
    )

    result = _run_helper(staging_root, instance)
    assert result.returncode == 0, result.stderr

    final_project = tmp_path / "final"
    staged_project.rename(final_project)

    _assert_console_script_runs(final_project / ".venv")
