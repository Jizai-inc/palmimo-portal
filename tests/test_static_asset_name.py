"""Pins the release asset's name across every place in this package that spells it out.

:data:`~palmimo_portal.core.update.STATIC_ASSET_NAME_TEMPLATE` is the single
source of truth for the frontend build's GitHub Release asset name. The
repository-root ``Makefile`` has to agree with it byte-for-byte, in its own
template syntax, or the Makefile's `fetch-static` convenience target
silently drifts from the Updater.

This file pins this literal on the Makefile side only (the template's own
rendering, and the Makefile's rendering) and checks the Makefile's
`fetch-static` recipe agrees on the tag flag. The
``.github/workflows/release.yml`` side of the same constant is pinned
separately, by ``tests/contracts/test_release_workflow_contract.py``.

:mod:`palmimo_portal.adapters.git_uv_updater` and
:mod:`palmimo_portal.fetch_static` both import the constant directly (see
``test_git_uv_updater_adapter.py`` and ``test_fetch_static.py``), so they
need no separate check here.
"""

from __future__ import annotations

import re
from pathlib import Path

from palmimo_portal.core.update import STATIC_ASSET_NAME_TEMPLATE


PORTAL_ROOT = Path(__file__).resolve().parents[1]

#: The literal both sides pin independently: the template's own rendering
#: for a shell ``${TAG}``-shaped placeholder, and the Makefile's rendering
#: for its own ``$(TAG)``-shaped placeholder (checked separately below).
_EXPECTED = "palmimo-portal-static-v1.2.3.tar.gz"


def test_static_asset_name_template_renders_the_expected_literal() -> None:
    assert STATIC_ASSET_NAME_TEMPLATE.format(tag="v1.2.3") == _EXPECTED


def test_makefile_static_asset_name_variable_names_the_asset_with_the_same_pattern() -> None:
    makefile_path = PORTAL_ROOT / "Makefile"
    assert makefile_path.is_file(), f"missing {makefile_path}"
    expected = STATIC_ASSET_NAME_TEMPLATE.format(tag="$(TAG)")

    definition_line = next(
        (
            line
            for line in makefile_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("STATIC_ASSET_NAME")
        ),
        None,
    )

    assert definition_line is not None, f"{makefile_path} must define STATIC_ASSET_NAME"
    assert expected in definition_line, (
        f"{makefile_path}'s STATIC_ASSET_NAME definition must name the release asset {expected!r} (from "
        "palmimo_portal.core.update.STATIC_ASSET_NAME_TEMPLATE) so it cannot drift from the Updater"
    )


def test_makefile_fetch_static_recipe_passes_the_tag_to_the_cli() -> None:
    makefile_path = PORTAL_ROOT / "Makefile"
    assert makefile_path.is_file(), f"missing {makefile_path}"

    text = makefile_path.read_text(encoding="utf-8")
    match = re.search(r"^fetch-static:\n((?:\t.*\n?)*)", text, re.MULTILINE)

    assert match is not None, f"{makefile_path} must define a fetch-static target"
    recipe = match.group(1)
    assert "palmimo_portal.fetch_static" in recipe, (
        f"{makefile_path}'s fetch-static recipe must invoke the palmimo_portal.fetch_static CLI"
    )
    assert re.search(r"--tag\s+\$\(TAG\)", recipe), (
        f"{makefile_path}'s fetch-static recipe must pass --tag $(TAG) to the palmimo_portal.fetch_static CLI"
    )
