"""Frontend third-party-license generation: wired into the build, verified in the release."""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE_PATH = REPO_ROOT / "Makefile"
RELEASE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"


def test_makefile_build_runs_licenses_after_npm_run_build() -> None:
    text = MAKEFILE_PATH.read_text(encoding="utf-8")
    match = re.search(r"^build:\n((?:\t.*\n?)*)", text, re.MULTILINE)
    assert match, f"{MAKEFILE_PATH} has no 'build:' target"
    lines = [line for line in match.group(1).splitlines() if line.strip()]
    build_index = next(i for i, line in enumerate(lines) if "run build" in line)
    licenses_index = next(i for i, line in enumerate(lines) if "run licenses" in line)
    assert licenses_index > build_index


def test_release_workflow_verifies_the_license_file_is_in_the_tarball() -> None:
    text = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert re.search(
        r"tar\s+-tzf\s+\"?\$asset\"?\s*\|\s*grep\s+-x\s+'static/THIRD_PARTY_LICENSES\.txt'\s*>/dev/null", text
    )
