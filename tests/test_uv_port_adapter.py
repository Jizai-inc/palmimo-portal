from __future__ import annotations

import subprocess
from typing import Any

from palmimo_portal.adapters.uv_port import UV_PYTHON_INSTALL_DIR, SubprocessUvPort


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if argv[2:4] == ["find", "--system"]:
            return subprocess.CompletedProcess(argv, 0, stdout="/usr/bin/python3\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_uv_port_prefers_system_python_then_installs_into_the_shared_directory() -> None:
    runner = _Runner()
    uv = SubprocessUvPort(runner=runner)

    assert uv.find_system_python(">=3.12") == "/usr/bin/python3"
    uv.install_python(">=3.13")

    find_argv, _ = runner.calls[0]
    assert find_argv[2:7] == ["find", "--system", "--no-project", "--no-config", ">=3.12"]
    install_argv, install_kwargs = runner.calls[1]
    assert install_argv[-2:] == [str(UV_PYTHON_INSTALL_DIR), ">=3.13"]
    assert install_kwargs["env"]["UV_PYTHON_INSTALL_DIR"] == str(UV_PYTHON_INSTALL_DIR)
