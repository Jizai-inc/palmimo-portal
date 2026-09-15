"""Tests for :mod:`palmimo_portal.adapters.git_port`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.adapters.git_port import SubprocessGitPort
from palmimo_portal.ports import GitCommandError


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        stdout = "deadbeef" if argv[1] == "rev-parse" else "deadbeef\trefs/heads/main\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_clone_shallow_places_a_double_dash_before_the_untrusted_url(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    port = SubprocessGitPort(runner=runner)

    port.clone_shallow("https://example.com/repo", "main", "branch", tmp_path / "dest")

    argv = runner.calls[0]
    assert argv[argv.index("--") + 1] == "https://example.com/repo"


def test_fetch_commit_places_a_double_dash_before_the_untrusted_url() -> None:
    runner = _RecordingRunner()
    port = SubprocessGitPort(runner=runner)

    port.fetch_commit("https://example.com/repo", "main", "branch")

    argv = runner.calls[0]
    assert argv[argv.index("--") + 1] == "https://example.com/repo"


class _FailingRunner:
    def __init__(self, stderr: str) -> None:
        self._stderr = stderr

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr=self._stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: could not read Username for 'https://example.com': terminal prompts disabled",
        "fatal: Authentication failed for 'https://example.com/repo'",
    ],
    ids=["prompt-disabled", "authentication-failed"],
)
def test_clone_shallow_maps_a_credential_helper_refusal_to_status_401(stderr: str, tmp_path: Path) -> None:
    # git's HTTP transport reports "returned error: 401" only when it actually reaches the
    # server; a rejected/expired credential helper refuses before that, with no numeric status --
    # without this mapping, core.periodic's credential-rejection handling never sees the 401/403
    # it watches for, so an expired credential is never flagged as rejected.
    runner = _FailingRunner(stderr)
    port = SubprocessGitPort(runner=runner)

    with pytest.raises(GitCommandError) as excinfo:
        port.clone_shallow("https://example.com/repo", "main", "branch", tmp_path / "dest")

    assert excinfo.value.status_code == 401
