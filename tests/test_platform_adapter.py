"""Tests for :mod:`palmimo_portal.adapters.platform`."""

from __future__ import annotations

import hashlib
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.adapters.platform import GitHubPlatformBundleSource, SudoPlatformPort
from palmimo_portal.ports import DisplayFirmwareState, PlatformCommandError, PlatformVerifyDiff


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, _max_bytes: int) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def test_fetch_latest_manifest_requests_the_asset_name_with_the_tag_as_is() -> None:
    # A tag like "v1.2.3" must reach the URL unmodified -- palmimo-image's release workflow
    # publishes assets named with the "v" included, not stripped.
    manifest_bytes = b'{"version": 1, "requires_portal": "0.0.0", "restart_portal": false, "summary": "s"}'
    sha_line = f"{hashlib.sha256(manifest_bytes).hexdigest()}  x\n".encode()
    requested_urls: list[str] = []

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        requested_urls.append(request.full_url)
        if request.full_url.endswith(".sha256"):
            return _FakeResponse(sha_line)
        return _FakeResponse(manifest_bytes)

    source = GitHubPlatformBundleSource(platform_repo="Jizai-inc/palmimo-image", opener=opener)

    source.fetch_latest_manifest("v1.2.3")

    assert requested_urls[0].endswith("palmimo-platform-v1.2.3.manifest.json")


class _ScriptedRunner:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr=self.stderr)


def test_verify_returns_diffs_when_install_sh_exits_1_with_differences(tmp_path: Path) -> None:
    # install.sh verify's own contract: exit 1 means "differences found", not "command failed" --
    # treating it as an error would silently drop every diff verify ever reports.
    runner = _ScriptedRunner(1, stdout='{"path": "/usr/lib/palmimo/app-launch", "kind": "missing"}\n')
    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", runner=runner)

    diffs = port.verify(tmp_path / "bundle")

    assert diffs == [PlatformVerifyDiff(path="/usr/lib/palmimo/app-launch", kind="missing")]


def test_verify_carries_expected_and_actual_from_a_mode_diff(tmp_path: Path) -> None:
    # A realistic verify_platform.py `_diff("mode", ...)` line -- the pair must survive parsing
    # so GET /platform and the not-ready log can show what was actually found on disk.
    line = '{"actual": "0640", "expected": "0644", "kind": "mode", "path": "/etc/polkit-1/rules.d/50-x.rules"}\n'
    runner = _ScriptedRunner(1, stdout=line)
    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", runner=runner)

    diffs = port.verify(tmp_path / "bundle")

    assert diffs == [
        PlatformVerifyDiff(path="/etc/polkit-1/rules.d/50-x.rules", kind="mode", expected="0644", actual="0640")
    ]


def test_verify_raises_platform_command_error_when_install_sh_exits_2_or_more(tmp_path: Path) -> None:
    runner = _ScriptedRunner(2, stderr="install.sh: unexpected error")
    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", runner=runner)

    with pytest.raises(PlatformCommandError):
        port.verify(tmp_path / "bundle")


def test_verify_wraps_install_sh_with_sudo_n_and_a_bounded_timeout_command(tmp_path: Path) -> None:
    runner = _ScriptedRunner(0, stdout="")
    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", runner=runner)
    bundle_dir = tmp_path / "bundle"

    port.verify(bundle_dir)

    assert runner.calls[0] == [
        "sudo",
        "-n",
        "timeout",
        "-k",
        "5",
        "60",
        str(bundle_dir / "install.sh"),
        "verify",
        "--root",
        "/",
    ]


@pytest.mark.parametrize(
    ("code", "result"),
    [
        (0, "updated"),
        (0, "current"),
        (0, "absent"),
        (0, "undeclared"),
        (0, "skipped"),
        (75, "busy"),
        (1, "failed"),
        (78, "failed"),
    ],
)
def test_display_firmware_parses_tool_results(tmp_path: Path, code: int, result: str) -> None:
    import json

    runner = _ScriptedRunner(code, stdout=json.dumps({"result": result, "before": "legacy", "after": None}))
    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", runner=runner)
    outcome = port.ensure_display_firmware()
    assert outcome.result == result
    assert outcome.before == "legacy"


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "[]",
        '{"result":"current"}\n{}',
        '{"result":"unknown"}',
        '{\n"result":"current"\n}',
        '{"result":"interrupted"}',
    ],
)
def test_display_firmware_rejects_malformed_tool_output(tmp_path: Path, stdout: str) -> None:
    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", runner=_ScriptedRunner(0, stdout=stdout))
    with pytest.raises(PlatformCommandError):
        port.ensure_display_firmware()


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("tool", 660), OSError("cannot launch")])
def test_display_firmware_wraps_execution_errors(tmp_path: Path, failure: Exception) -> None:
    def run(*args: Any, **kwargs: Any) -> None:
        raise failure

    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", runner=run)
    with pytest.raises(PlatformCommandError):
        port.ensure_display_firmware()


def test_display_firmware_detects_missing_tool_and_unreadable_state(tmp_path: Path) -> None:
    port = SudoPlatformPort(
        installed_path=tmp_path / "installed.json",
        display_firmware_tool=tmp_path / "tool",
        display_firmware_state_path=tmp_path / "state.json",
    )
    assert not port.has_display_firmware_tool()
    assert port.read_display_firmware_state() is None
    (tmp_path / "tool").touch()
    assert port.has_display_firmware_tool()
    (tmp_path / "state.json").write_text('{"last_result":"current","last_reason":null,"seen":true}')
    assert port.read_display_firmware_state() == DisplayFirmwareState(last_result="current")
    (tmp_path / "state.json").write_text("broken")
    assert port.read_display_firmware_state() is None


def test_display_firmware_invalid_json_keeps_stderr_reason(firmware_process: tuple[SudoPlatformPort, Path]) -> None:
    import sys

    port, script = firmware_process
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        'print("Traceback:", file=sys.stderr, flush=True)\n'
        'print("OSError: firmware archive is unreadable", file=sys.stderr, flush=True)\n'
        "sys.exit(1)\n"
    )
    with pytest.raises(PlatformCommandError, match="OSError: firmware archive is unreadable"):
        port.ensure_display_firmware()


@pytest.mark.parametrize("content", [None, b"", b"boot-a\n", b"\xff"])
def test_boot_identity_is_optional_when_kernel_file_is_unavailable(tmp_path: Path, content: bytes | None) -> None:
    path = tmp_path / "boot_id"
    if content is not None:
        path.write_bytes(content)
    port = SudoPlatformPort(installed_path=tmp_path / "installed.json", boot_id_path=path)
    assert port.read_boot_id() == ("boot-a" if content == b"boot-a\n" else None)


@pytest.fixture
def firmware_process(tmp_path: Path) -> tuple[SudoPlatformPort, Path]:
    wrapper = tmp_path / "privilege-wrapper"
    wrapper.write_text('#!/bin/sh\nshift 5\nexec "$@"\n')
    wrapper.chmod(0o755)
    script = tmp_path / "firmware-tool"
    script.touch()
    script.chmod(0o755)
    return SudoPlatformPort(
        installed_path=tmp_path / "installed.json",
        sudo_bin=str(wrapper),
        display_firmware_tool=script,
    ), script


def test_display_firmware_logs_progress_before_tool_finishes(
    firmware_process: tuple[SudoPlatformPort, Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    import sys
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from palmimo_portal.ports import DisplayFirmwareResult

    port, script = firmware_process
    release = tmp_path / "release"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        'print("flash in progress", file=sys.stderr, flush=True)\n'
        f"while not pathlib.Path({str(release)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        'print(\'{"result":"current"}\', flush=True)\n'
    )
    logged = threading.Event()

    class ProgressHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "flash in progress" in record.getMessage():
                logged.set()

    handler = ProgressHandler()
    logger = logging.getLogger("palmimo_portal")
    caplog.set_level(logging.INFO, logger="palmimo_portal")
    logger.addHandler(handler)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(port.ensure_display_firmware)
            try:
                assert logged.wait(3)
                assert not future.done()
            finally:
                release.touch()
            assert future.result(timeout=3) == DisplayFirmwareResult(result="current")
    finally:
        logger.removeHandler(handler)


def test_display_firmware_timeout_stops_and_reaps_tool(
    firmware_process: tuple[SudoPlatformPort, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import sys
    import time

    from palmimo_portal.adapters import platform

    port, script = firmware_process
    pid_file = tmp_path / "pid"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        'print("waiting for flash", file=sys.stderr, flush=True)\n'
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(platform, "DISPLAY_FIRMWARE_SUBPROCESS_TIMEOUT_SECONDS", 1.0)
    started = time.monotonic()
    with pytest.raises(PlatformCommandError, match="timed out"):
        port.ensure_display_firmware()
    assert time.monotonic() - started < 5
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
