"""Real :class:`~palmimo_portal.ports.UvPort`: ``uv --version`` via subprocess, no shell."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

from palmimo_portal.ports import UvPort


logger = logging.getLogger("palmimo_portal")

VERSION_TIMEOUT_SECONDS = 5.0


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
