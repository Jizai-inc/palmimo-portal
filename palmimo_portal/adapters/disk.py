"""Real :class:`~palmimo_portal.ports.DiskPort`: ``os.statvfs``."""

from __future__ import annotations

import os
from pathlib import Path

from palmimo_portal.ports import DiskPort


class OsDiskPort(DiskPort):
    def free_bytes(self, path: Path) -> int:
        # statvfs needs an existing path; an app job's disk precheck runs
        # against `apps_dir`, which the deploy contract guarantees exists
        # (StateDirectory=palmimo -- see design doc 2.1).
        stats = os.statvfs(path)
        return stats.f_bavail * stats.f_frsize
