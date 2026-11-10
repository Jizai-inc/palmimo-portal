from dataclasses import replace

import pytest

from palmimo_portal.core.legacy_official import legacy_official
from palmimo_portal.ports import AppSource


@pytest.mark.parametrize(
    "subdir, manifest, target",
    [
        ("examples/agents/companion", "palmimo.realtime.toml", "companion-realtime"),
        ("examples/teleop", None, "teleop"),
        ("examples/teleop", "palmimo.toml", "teleop"),
        ("examples/agents/companion", None, None),
        ("examples/agents/wakeword", "palmimo.toml", None),
    ],
)
@pytest.mark.parametrize(
    "variant", ["original", "normalized_url", "opted_out", "other_ref", "branch", "other_repo", "other_manifest", "zip"]
)
def test_legacy_official_matches_only_shipped_sources(
    subdir: str, manifest: str | None, target: str | None, variant: str
) -> None:
    source = AppSource(
        "git", "https://github.com/Jizai-inc/palmimo-devkit", "examples-v0.1.0", "tag", subdir, "a" * 40, manifest
    )
    if variant == "normalized_url":
        source = replace(source, url="HTTPS://GITHUB.COM/JIZAI-INC/PALMIMO-DEVKIT.git/")
    elif variant == "opted_out":
        source = replace(source, public_app_id="")
    elif variant == "other_ref":
        source = replace(source, ref="v1")
    elif variant == "branch":
        source = replace(source, ref_kind="branch")
    elif variant == "other_repo":
        source = replace(source, url="https://github.com/alice/palmimo-devkit")
    elif variant == "other_manifest":
        source = replace(source, manifest="custom.toml")
    elif variant == "zip":
        source = replace(source, type="zip")
    match = legacy_official(source)
    if variant in ("original", "normalized_url"):
        assert match is not None
        assert match.name == target
    else:
        assert match is None
