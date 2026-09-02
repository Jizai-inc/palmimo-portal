"""Holds the frontend third-party-license generation step wired into the build.

The Makefile's `build` target and `frontend/package.json`'s `licenses` script
are the only two places that know the generated notice's output path
(`palmimo_portal/static/THIRD_PARTY_LICENSES.txt`); this contract checks both
agree with each other and that `build` actually invokes `licenses`, so a
future edit to either cannot silently stop the release tarball from carrying
attribution for its bundled MIT/BSD dependencies.
"""

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE_PATH = REPO_ROOT / "Makefile"
PACKAGE_JSON_PATH = REPO_ROOT / "frontend" / "package.json"
GENERATOR_SCRIPT_PATH = REPO_ROOT / "frontend" / "scripts" / "generate-third-party-licenses.mjs"

EXPECTED_OUTPUT_RELATIVE_TO_FRONTEND = "../palmimo_portal/static/THIRD_PARTY_LICENSES.txt"


def _build_target_body() -> str:
    text = MAKEFILE_PATH.read_text(encoding="utf-8")
    match = re.search(r"^build:\n((?:\t.*\n?)*)", text, re.MULTILINE)
    assert match, f"{MAKEFILE_PATH} has no 'build:' target"
    return match.group(1)


def test_generator_script_exists() -> None:
    assert GENERATOR_SCRIPT_PATH.is_file(), f"missing {GENERATOR_SCRIPT_PATH}"


def test_package_json_licenses_script_writes_to_static() -> None:
    package_json = json.loads(PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
    scripts = package_json.get("scripts", {})
    assert "licenses" in scripts, f"{PACKAGE_JSON_PATH} must declare a 'licenses' script"

    licenses_script = scripts["licenses"]
    assert "generate-third-party-licenses.mjs" in licenses_script
    assert EXPECTED_OUTPUT_RELATIVE_TO_FRONTEND in licenses_script, (
        f"{PACKAGE_JSON_PATH}'s 'licenses' script must write to "
        f"{EXPECTED_OUTPUT_RELATIVE_TO_FRONTEND} (relative to frontend/), matching where "
        "release.yml's tarball step (`tar -C palmimo_portal -czf ... static`) picks it up"
    )


def test_makefile_build_target_runs_licenses_script() -> None:
    build_body = _build_target_body()
    assert re.search(r"\bnpm run licenses\b|\brun licenses\b", build_body), (
        f"{MAKEFILE_PATH}'s 'build' target must run the frontend's 'licenses' npm script "
        "so every build produces palmimo_portal/static/THIRD_PARTY_LICENSES.txt"
    )


def test_makefile_build_target_runs_licenses_after_frontend_build() -> None:
    build_body = _build_target_body()
    lines = [line for line in build_body.splitlines() if line.strip()]
    build_index = next(i for i, line in enumerate(lines) if "run build" in line)
    licenses_index = next(i for i, line in enumerate(lines) if "run licenses" in line)
    assert licenses_index > build_index, (
        "the licenses script must run after the frontend build step, since it lands its output "
        "under the static/ directory the frontend build (re)creates"
    )
