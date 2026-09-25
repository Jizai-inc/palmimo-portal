"""Real :class:`~palmimo_portal.ports.GitPort`: ``git clone``/``fetch`` via subprocess, no shell.

Same subprocess style as :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`:
argv lists (never a shell string), a bounded timeout, and stderr masked
through :func:`~palmimo_portal.core.secrets.mask_authorization_lines` before
it ever reaches a log line or exception message.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from palmimo_portal.core.secrets import mask_authorization_lines
from palmimo_portal.ports import AppRefKind, GitCommandError, GitPort


logger = logging.getLogger("palmimo_portal")

CLONE_TIMEOUT_SECONDS = 300.0
FETCH_TIMEOUT_SECONDS = 60.0

Runner = subprocess.run

#: git's own HTTP transport reports an auth failure as "...returned error: 401"
#: (or 403) in stderr -- there is no structured exit-code signal for it.
_HTTP_STATUS_RE = re.compile(r"returned error:\s*(\d{3})")

#: A rejected/expired credential can also surface with no numeric status at all -- git's
#: credential-helper layer refuses before ever making the HTTP request, or the server closes
#: the connection outright. These substrings are git's own wording for that case (lowercased
#: match): still an auth failure, so treated the same as an explicit "401".
_AUTH_FAILURE_MARKERS = ("could not read username", "terminal prompts disabled", "authentication failed")


def _failure_reason(stderr: str, status: int | None) -> str:
    lowered = stderr.lower()
    if status in (401, 403):
        return "git_credential_rejected" if status == 403 else "git_credential_missing"
    if "not found" in lowered or "does not appear to be a git repository" in lowered:
        return "git_not_found"
    if any(
        marker in lowered
        for marker in ("could not resolve host", "network is unreachable", "failed to connect", "connection timed out")
    ):
        return "git_network_unreachable"
    return "git_unknown"


def _parse_http_status(stderr: str) -> int | None:
    match = _HTTP_STATUS_RE.search(stderr)
    if match:
        return int(match.group(1))
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return 401
    return None


@dataclass
class SubprocessGitPort(GitPort):
    runner: object = field(default=subprocess.run)

    def clone_shallow(
        self,
        url: str,
        ref: str,
        ref_kind: AppRefKind,
        dest: Path,
        *,
        env: Mapping[str, str] | None = None,
        blobless: bool = False,
        sparse_subdir: str | None = None,
    ) -> str:
        # `--` stops option parsing before `url`/`dest` -- both are untrusted (see
        # `palmimo_portal.core.apps.validate_git_url`/`validate_git_ref`, which already
        # reject a leading `-`); this is the defense-in-depth half at the argv boundary.
        argv = ["git", "clone", "--depth", "1"]
        if blobless:
            # `--sparse` checks out only the top-level files at first; the
            # `sparse-checkout set` below widens that to include `sparse_subdir`
            # and materializes it -- unlike `--no-checkout`, this never leaves
            # the working tree empty.
            argv.extend(["--filter=blob:none", "--sparse"])
        argv.extend(["--branch", ref, "--", url, str(dest)])
        self._run(argv, cwd=None, timeout=CLONE_TIMEOUT_SECONDS, env=env)
        if sparse_subdir is not None:
            self._run(
                ["git", "sparse-checkout", "set", "--cone", "--", sparse_subdir],
                cwd=dest,
                timeout=CLONE_TIMEOUT_SECONDS,
                env=env,
            )
        return self._rev_parse(dest, env=env)

    def fetch_commit(self, url: str, ref: str, ref_kind: AppRefKind, *, env: Mapping[str, str] | None = None) -> str:
        # `git ls-remote` needs no local checkout at all -- cheaper than a clone for a check.
        result = self._run(["git", "ls-remote", "--", url, ref], cwd=None, timeout=FETCH_TIMEOUT_SECONDS, env=env)
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        commit = line.split()[0] if line else ""
        if not commit:
            raise GitCommandError(f"ref {ref!r} not found on {url}", reason="git_not_found")
        return commit

    def _rev_parse(self, dest: Path, *, env: Mapping[str, str] | None) -> str:
        result = self._run(["git", "rev-parse", "HEAD"], cwd=dest, timeout=FETCH_TIMEOUT_SECONDS, env=env)
        return result.stdout.strip()

    def _run(
        self, argv: list[str], *, cwd: Path | None, timeout: float, env: Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(  # type: ignore[operator]
                argv,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **env} if env is not None else None,
            )
        except subprocess.TimeoutExpired as error:
            raise GitCommandError(
                f"{' '.join(argv[:2])} timed out after {timeout:g}s", reason="git_network_unreachable"
            ) from error
        except OSError as error:
            raise GitCommandError(
                f"{' '.join(argv[:2])} failed to start: {error}", reason="git_network_unreachable"
            ) from error
        if result.returncode != 0:
            raw_stderr = (result.stderr or "").strip()
            tail = mask_authorization_lines(raw_stderr)
            raise GitCommandError(
                f"{' '.join(argv[:2])} exited {result.returncode}: {tail}",
                status_code=_parse_http_status(raw_stderr),
                reason=_failure_reason(raw_stderr, _parse_http_status(raw_stderr)),
            )
        return result
