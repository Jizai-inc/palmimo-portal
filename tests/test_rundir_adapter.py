"""The run directory's modes are fixed by the adapter, not by the process umask."""

import json
import os
import stat
from pathlib import Path

import pytest

from palmimo_portal.adapters.rundir import TmpfsRunDirPort


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_write_gives_the_app_directory_and_argv_group_readable_modes_under_any_umask(
    tmp_path: Path, umask: int
) -> None:
    # Another thread can hold a restrictive umask while this runs; the app account reads
    # argv.json through the directory, so both modes must not depend on it.
    previous = os.umask(umask)
    try:
        TmpfsRunDirPort(tmp_path).write("app", env={"A": "1"}, argv=["x"], cwd="/tmp", project="/tmp", python=">=3.12")
    finally:
        os.umask(previous)

    assert stat.S_IMODE((tmp_path / "app").stat().st_mode) == 0o750
    assert stat.S_IMODE((tmp_path / "app" / "argv.json").stat().st_mode) == 0o640
    assert stat.S_IMODE((tmp_path / "app" / "env").stat().st_mode) == 0o600
    assert json.loads((tmp_path / "app" / "argv.json").read_text()) == {
        "argv": ["x"],
        "cwd": "/tmp",
        "project": "/tmp",
        "python": ">=3.12",
    }
