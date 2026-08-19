"""CLI: download, verify, and install one release's frontend build into ``static/``.

For a developer or tester who wants the Portal's built UI without installing
Node -- what the repository-root ``Makefile``'s ``fetch-static`` target
shells out to. Calls the same download/verify/extract/swap functions as
:class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`'s
``assets``/``install-assets`` steps (see
:mod:`palmimo_portal.adapters.static_asset`) -- one implementation of "fetch
a release's frontend asset safely", not two.

Usage::

    uv run python -m palmimo_portal.fetch_static --tag vX.Y.Z
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from palmimo_portal.adapters.static_asset import StaticAssetError, default_opener, fetch_and_stage, swap_into_place
from palmimo_portal.core.update import STATIC_ASSET_NAME_TEMPLATE
from palmimo_portal.settings import DEFAULT_STATIC_DIR, DEFAULT_UPDATE_REPO
from palmimo_portal.version import portal_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="palmimo_portal.fetch_static",
        description="Download, verify, and install a Palmimo Portal release's frontend build into static/.",
    )
    parser.add_argument("--tag", required=True, help="Release tag to fetch, e.g. v1.2.3")
    parser.add_argument(
        "--repo",
        default=DEFAULT_UPDATE_REPO,
        help=f"GitHub 'owner/repo' to fetch the release from (default: {DEFAULT_UPDATE_REPO})",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_STATIC_DIR,
        help=f"Destination static/ directory, replaced in place (default: {DEFAULT_STATIC_DIR})",
    )
    return parser


def fetch_static(tag: str, repo: str, dest: Path) -> None:
    """Download, verify, and install ``tag``'s frontend build into ``dest``, replacing it.

    Stages into a sibling ``static.tmp-<pid>`` directory first (via
    :func:`~palmimo_portal.adapters.static_asset.fetch_and_stage`) and only
    swaps it into ``dest`` once the download and safety checks have fully
    succeeded (via :func:`~palmimo_portal.adapters.static_asset.swap_into_place`)
    -- the same two-phase shape as a device update, so a failed fetch never
    leaves ``dest`` half replaced.

    Raises:
        StaticAssetError: the download, checksum, extraction, or swap failed.
    """
    asset_name = STATIC_ASSET_NAME_TEMPLATE.format(tag=tag)
    temp_dir = dest.parent / f"static.tmp-{os.getpid()}"
    not_found_message = f"release {tag} has no frontend asset {asset_name} in {repo}"
    fetch_and_stage(
        default_opener,
        repo,
        tag,
        asset_name,
        f"palmimo-portal/{portal_version()}",
        temp_dir,
        not_found_message=not_found_message,
    )
    swap_into_place(temp_dir, dest)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fetch_static(args.tag, args.repo, args.dest)
    except StaticAssetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Fetched {args.tag} into {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
