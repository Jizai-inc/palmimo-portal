"""Contract: no module invokes ``uv sync``/``uv lock`` argv except through the app-sync unit (design doc 2.2b).

Dependency sync for an app runs as ``palmimo-app`` inside the
``palmimo-app-sync@`` unit, never in the Portal's own process -- an
untrusted app's ``pyproject.toml`` build backend or path dependency could
otherwise run arbitrary code with the Portal's own privileges. A stray
direct ``uv sync``/``uv lock`` subprocess call site (bypassing
``SyncUnitPort``) would reopen that hole silently; this scan is the only
thing that would notice.
"""

from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "palmimo_portal"

#: - adapters/uv_port.py -- `UvPort`'s only remaining call is `uv --version`.
#: - adapters/git_uv_updater.py -- syncs the *Portal's own* checkout on self-update, a trusted
#:   tree the Portal maintains, unrelated to the untrusted-app sandboxing design doc 2.2b covers.
ALLOWED_FILES = {"adapters/uv_port.py", "adapters/git_uv_updater.py"}

_UV_MARKERS = ('"uv"', "'uv'", "uv_bin")
_SYNC_OR_LOCK_ARGV_MARKERS = ('"sync"', "'sync'", '"lock"', "'lock'")


def _invokes_uv_sync_or_lock(line: str) -> bool:
    return any(marker in line for marker in _UV_MARKERS) and any(
        marker in line for marker in _SYNC_OR_LOCK_ARGV_MARKERS
    )


def test_uv_sync_and_lock_argv_are_referenced_only_in_the_allowed_uv_adapters() -> None:
    offenders = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative in ALLOWED_FILES:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if _invokes_uv_sync_or_lock(line):
                offenders.append(relative)
                break
    assert offenders == [], f"'uv sync'/'uv lock' argv referenced outside the allowed adapters: {offenders}"
