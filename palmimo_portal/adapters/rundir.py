"""Real :class:`~palmimo_portal.ports.RunDirPort`: ``/run/palmimo/apps/<name>/`` on tmpfs.

Schema (design doc 2.1/3.5 step 6)::

    <run_dir>/<name>/         user:palmimo-apps 0750
      env                     KEY=value lines, 0600 (root-readable only -- PID1's EnvironmentFile=)
      argv.json               {"argv": [...], "cwd": ..., "project": ...}, 0640

``tmpfiles.d`` (not this module) creates ``<run_dir>`` itself and its
``0755`` mode at boot; this adapter only manages the per-app subdirectory.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
from pathlib import Path

from palmimo_portal.core.os_group import apps_gid
from palmimo_portal.ports import RunDirPort


logger = logging.getLogger("palmimo_portal")

_ENV_FILENAME = "env"
_ARGV_FILENAME = "argv.json"


class TmpfsRunDirPort(RunDirPort):
    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir

    def _app_dir(self, name: str) -> Path:
        return self._run_dir / name

    def write(self, name: str, *, env: dict[str, str], argv: list[str], cwd: str, project: str) -> None:
        app_dir = self._app_dir(name)
        shutil.rmtree(app_dir, ignore_errors=True)
        app_dir.mkdir(parents=True, exist_ok=True)
        # Set explicitly: mkdir's mode is narrowed by the process umask, which
        # another thread may have tightened at that moment (atomic_write did).
        app_dir.chmod(0o750)

        env_path = app_dir / _ENV_FILENAME
        env_text = "".join(f"{key}={value}\n" for key, value in env.items())
        env_path.write_text(env_text, encoding="utf-8")
        env_path.chmod(0o600)

        argv_path = app_dir / _ARGV_FILENAME
        argv_path.write_text(json.dumps({"argv": argv, "cwd": cwd, "project": project}), encoding="utf-8")
        argv_path.chmod(0o640)

        gid = apps_gid()
        if gid is not None:
            with contextlib.suppress(OSError):
                os.chown(app_dir, -1, gid)
                os.chown(argv_path, -1, gid)

    def remove(self, name: str) -> None:
        shutil.rmtree(self._app_dir(name), ignore_errors=True)

    def remove_all(self) -> None:
        # Only the per-app subdirectories are removed, never `self._run_dir`
        # itself -- `tmpfiles.d` owns creating that with its own mode/owner.
        for child in self._run_dir.glob("*"):
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
