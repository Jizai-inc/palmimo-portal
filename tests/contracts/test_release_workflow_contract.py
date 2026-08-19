"""Holds the release workflow's shape steady: trigger, guardrails, and asset naming.

The workflow (`.github/workflows/release.yml`) and
:data:`~palmimo_portal.core.update.STATIC_ASSET_NAME_TEMPLATE` live in this
one repository, so this contract imports the template directly and checks
the workflow's literal asset name -- both the tarball and its `.sha256`
sidecar -- against it, rather than pinning a second, independent copy of
the pattern here. It also checks the guardrails that make a release safe:
the tag trigger, the on-main guard, and the published-release guard.

Each check is scoped to the specific line or shell block that actually
carries the thing being asserted -- not "does this string appear anywhere in
the file" -- so a stray comment could not make a real drift pass silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]  # no types-PyYAML dev dependency; see pyproject.toml's dev group

from palmimo_portal.core.update import STATIC_ASSET_NAME_TEMPLATE


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"
RELEASING_GUIDE_PATH = REPO_ROOT / "doc" / "releasing.md"

#: Matches a YAML step's ``run: |`` block: the block scalar indicator line,
#: then every following line indented deeper than it, up to (but not
#: including) the next line at the same or shallower indentation. Good
#: enough for this one workflow file's straightforward 6-space step
#: indentation -- not a general YAML parser.
_RUN_BLOCK_PATTERN = re.compile(r"^( *)run: \|\n((?:\1 .*\n?)*)", re.MULTILINE)

#: The template rendered for a shell `${TAG}`-shaped placeholder -- the
#: workflow's own rendering, checked against the same source of truth the
#: Makefile's rendering is checked against in
#: tests/test_static_asset_name.py.
_ASSET_TEMPLATE = STATIC_ASSET_NAME_TEMPLATE.format(tag="${TAG}")
_SHA256_TEMPLATE = f"{_ASSET_TEMPLATE}.sha256"


def _run_blocks(workflow_text: str) -> list[str]:
    """Return the body text of every ``run: |`` block in the workflow."""
    return [match.group(2) for match in _RUN_BLOCK_PATTERN.finditer(workflow_text)]


def test_release_workflow_triggers_on_version_tags() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    # PyYAML parses the bare `on:` key as the boolean `True`.
    on_section = workflow.get("on") or workflow.get(True)
    assert on_section is not None, f"{WORKFLOW_PATH} has no 'on:' trigger section"
    tags = on_section.get("push", {}).get("tags", [])
    assert "v*" in tags, f"{WORKFLOW_PATH} must trigger on 'v*' tag pushes, got {tags!r}"


def test_release_workflow_refuses_a_tag_off_main() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    guard_blocks = [block for block in blocks if "merge-base" in block and "--is-ancestor" in block]
    assert guard_blocks, f"{WORKFLOW_PATH} must verify the tagged commit is an ancestor of main"
    assert any("origin/main" in block for block in guard_blocks), (
        f"{WORKFLOW_PATH}'s on-main guard must check ancestry against origin/main"
    )


def test_release_workflow_refuses_to_touch_a_published_release() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    guard_blocks = [block for block in blocks if "isDraft" in block]
    assert guard_blocks, f"{WORKFLOW_PATH} must check whether an existing release is a draft before touching it"
    assert any("already published" in block for block in guard_blocks), (
        f"{WORKFLOW_PATH} must refuse to modify an already-published release"
    )


def test_release_workflow_creates_a_draft_with_generated_notes() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    create_blocks = [block for block in blocks if "gh release create" in block]
    assert create_blocks, f"{WORKFLOW_PATH} has no 'gh release create' run block to check"
    assert any("--draft" in block and "--generate-notes" in block for block in create_blocks), (
        f"{WORKFLOW_PATH}'s 'gh release create' invocation must use --draft --generate-notes"
    )


def test_release_workflow_marks_a_hyphenated_tag_as_a_prerelease() -> None:
    # A tag like v1.2.0-rc1 must never surface as `releases/latest` (what
    # every device's Updater queries) by a maintainer forgetting to tick
    # the "pre-release" box by hand -- see doc/releasing.md's Versioning
    # section.
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    create_blocks = [block for block in blocks if "gh release create" in block]
    assert create_blocks, f"{WORKFLOW_PATH} has no 'gh release create' run block to check"
    assert any("--prerelease" in block for block in create_blocks), (
        f"{WORKFLOW_PATH} must pass --prerelease for a hyphenated (pre-release) tag"
    )


def test_release_workflow_tar_line_names_the_asset_with_the_expected_pattern() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    tar_blocks = [block for block in blocks if re.search(r"tar -C .*-czf", block)]

    assert tar_blocks, f"{WORKFLOW_PATH} has no 'tar -C ... -czf' run block to check"
    assert any(_ASSET_TEMPLATE in block for block in tar_blocks), (
        f"the run: block packaging the release asset in {WORKFLOW_PATH} must name it {_ASSET_TEMPLATE!r} "
        f"(from palmimo_portal.core.update.STATIC_ASSET_NAME_TEMPLATE)"
    )


def test_release_workflow_tar_rooted_the_asset_under_static() -> None:
    # palmimo_portal/adapters/static_asset.py's extractor refuses any member
    # not rooted under `static/` -- the tar invocation must keep producing
    # members with that prefix.
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    tar_blocks = [block for block in blocks if re.search(r"tar -C .*-czf", block)]
    assert tar_blocks, f"{WORKFLOW_PATH} has no 'tar -C ... -czf' run block to check"
    assert any(re.search(r"tar -C palmimo_portal -czf .*\bstatic\b", block) for block in tar_blocks), (
        f"{WORKFLOW_PATH}'s tar invocation must package palmimo_portal/static/ with members rooted at 'static/'"
    )


def test_release_workflow_gh_release_lines_name_the_asset_with_the_expected_pattern() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    gh_blocks = [block for block in blocks if "gh release create" in block or "gh release upload" in block]

    assert gh_blocks, f"{WORKFLOW_PATH} has no 'gh release create'/'gh release upload' run block to check"
    assert any(_ASSET_TEMPLATE in block for block in gh_blocks), (
        f"the run: block creating/uploading the release in {WORKFLOW_PATH} must name the asset {_ASSET_TEMPLATE!r}"
    )


def test_release_workflow_sha256_sidecar_matches_the_expected_pattern() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    sha_blocks = [block for block in blocks if "sha256sum" in block]
    assert sha_blocks, f"{WORKFLOW_PATH} has no 'sha256sum' run block to check"
    # The sidecar is named from the same shell `$asset` variable the tar
    # step derives from `_ASSET_TEMPLATE` (checked above), not a second,
    # independently-spelled literal -- so this only needs to confirm the
    # sidecar suffix is derived from that variable, not hardcoded.
    assert any('"$asset.sha256"' in block for block in sha_blocks), (
        f"the run: block hashing the release asset in {WORKFLOW_PATH} must derive the sidecar name from "
        f"the same $asset variable as the tar step, as '$asset.sha256'"
    )


def test_releasing_guide_mentions_the_asset_pattern() -> None:
    assert RELEASING_GUIDE_PATH.is_file(), f"missing {RELEASING_GUIDE_PATH}"
    text = RELEASING_GUIDE_PATH.read_text(encoding="utf-8")
    assert "palmimo-portal-static-<tag>.tar.gz" in text or "palmimo-portal-static-vX.Y.Z.tar.gz" in text, (
        f"{RELEASING_GUIDE_PATH} must describe the release asset's name pattern"
    )
