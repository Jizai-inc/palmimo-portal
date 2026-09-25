"""Real :class:`~palmimo_portal.ports.UvPort`: ``uv --version`` via subprocess, no shell."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from palmimo_portal.ports import UvPort, UvPythonInstallError


logger = logging.getLogger("palmimo_portal")

VERSION_TIMEOUT_SECONDS = 5.0
PYTHON_TIMEOUT_SECONDS = 300.0
UV_PYTHON_INSTALL_DIR = Path("/var/lib/palmimo/uv-python")


@dataclass
class SubprocessUvPort(UvPort):
    uv_bin: str = "uv"
    runner: object = field(default=subprocess.run)

    def version(self) -> str:
        try:
            result = self.runner(  # type: ignore[operator]
                [self.uv_bin, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            logger.warning("uv version: %s --version failed: %s", self.uv_bin, error)
            return "unknown"
        return (result.stdout or "").strip() or "unknown"

    def find_system_python(self, requirement: str) -> str | None:
        env = os.environ.copy()
        env["UV_PYTHON_INSTALL_DIR"] = str(UV_PYTHON_INSTALL_DIR)
        env["UV_PYTHON_PREFERENCE"] = "system"
        env["UV_PYTHON_DOWNLOADS"] = "never"
        try:
            result = self.runner(  # type: ignore[operator]
                [self.uv_bin, "python", "find", "--system", "--no-project", "--no-config", requirement],
                capture_output=True,
                text=True,
                timeout=PYTHON_TIMEOUT_SECONDS,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            raise UvPythonInstallError(f"could not search for system Python {requirement!r}: {error}") from error
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None

    def install_python(self, requirement: str) -> None:
        env = os.environ.copy()
        env["UV_PYTHON_INSTALL_DIR"] = str(UV_PYTHON_INSTALL_DIR)
        try:
            result = self.runner(  # type: ignore[operator]
                [
                    self.uv_bin,
                    "python",
                    "install",
                    "--no-config",
                    "--install-dir",
                    str(UV_PYTHON_INSTALL_DIR),
                    requirement,
                ],
                capture_output=True,
                text=True,
                timeout=PYTHON_TIMEOUT_SECONDS,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            raise UvPythonInstallError(f"could not install Python {requirement!r}: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "uv exited unsuccessfully").strip()
            raise UvPythonInstallError(f"could not install Python {requirement!r}: {detail}")
