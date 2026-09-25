from __future__ import annotations

import subprocess
from typing import Any

from palmimo_portal.adapters.uv_port import UV_PYTHON_INSTALL_DIR, SubprocessUvPort


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        env = kwargs.get("env", {})
        if (
            argv[2:4] == ["find", "--system"]
            and env.get("UV_PYTHON_INSTALL_DIR") == str(UV_PYTHON_INSTALL_DIR)
            and env.get("UV_PYTHON_PREFERENCE") == "system"
            and env.get("UV_PYTHON_DOWNLOADS") == "never"
        ):
            return subprocess.CompletedProcess(argv, 0, stdout="/usr/bin/python3\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")


def test_uv_port_finds_only_the_python_visible_to_the_sync_unit() -> None:
    runner = _Runner()
    uv = SubprocessUvPort(runner=runner)

    assert uv.find_system_python(">=3.12") == "/usr/bin/python3"
