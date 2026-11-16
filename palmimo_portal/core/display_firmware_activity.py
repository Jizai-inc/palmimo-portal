from __future__ import annotations

import threading

from palmimo_portal.ports import DisplayFirmwareResult, PlatformPort


class DisplayFirmwareActivity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writers = 0

    @property
    def active(self) -> bool:
        with self._lock:
            return self._writers > 0

    def ensure(self, platform: PlatformPort) -> DisplayFirmwareResult:
        with self._lock:
            self._writers += 1
        try:
            return platform.ensure_display_firmware()
        finally:
            with self._lock:
                self._writers -= 1
