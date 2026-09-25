"""Contract: ``sudo`` is confined to one file (design doc 2.8's "Portal calls sudo nowhere else").

A stray second call site would run arbitrary root commands outside the
one audited path (``install.sh verify``/``install``/``record``) -- this
scan is the only thing that would notice a future PR adding one.
"""

from __future__ import annotations

import re
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "palmimo_portal"

#: Files allowed to mention `sudo_bin` or the literal `"sudo"`:
#: - settings.py -- the field declaration.
#: - wiring.py -- pure DI plumbing (every Settings field is threaded through
#:   here into its adapter's constructor; sudo_bin is no exception).
#: - adapters/platform.py -- the one adapter that actually shells out through it.
#: - ports.py -- PlatformPort's docstring explains *why* this is the one port
#:   whose real implementation calls sudo; no runtime code there touches it.
ALLOWED_FILES = {"settings.py", "wiring.py", "adapters/platform.py", "ports.py"}

_SUDO_PATTERN = re.compile(r"sudo_bin|[\"']sudo[\"']")


def test_sudo_is_referenced_only_in_settings_and_the_platform_adapter() -> None:
    offenders = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative in ALLOWED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if _SUDO_PATTERN.search(text):
            offenders.append(relative)
    assert offenders == [], f"sudo_bin/'sudo' referenced outside the allowed files: {offenders}"
