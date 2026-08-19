"""Tests for :mod:`palmimo_portal.fetch_static`.

``fetch_static`` itself is a thin wrapper around
:func:`~palmimo_portal.adapters.static_asset.fetch_and_stage` and
:func:`~palmimo_portal.adapters.static_asset.swap_into_place` -- the same
functions :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater` calls
for a device update -- so these tests only need to prove the CLI parses its
arguments correctly and calls through to the shared machinery with them,
not re-exercise the download/verify/extract/swap behavior itself (that is
``test_git_uv_updater_adapter.py``'s job, against the same underlying
functions).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from palmimo_portal import fetch_static
from palmimo_portal.adapters.static_asset import StaticAssetError
from palmimo_portal.core.update import STATIC_ASSET_NAME_TEMPLATE
from palmimo_portal.settings import DEFAULT_STATIC_DIR, DEFAULT_UPDATE_REPO


def test_build_parser_requires_tag() -> None:
    parser = fetch_static.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_build_parser_defaults_repo_and_dest_from_settings() -> None:
    parser = fetch_static.build_parser()

    args = parser.parse_args(["--tag", "v1.2.3"])

    assert args.tag == "v1.2.3"
    assert args.repo == DEFAULT_UPDATE_REPO
    assert args.dest == DEFAULT_STATIC_DIR


def test_build_parser_accepts_repo_and_dest_overrides(tmp_path: Path) -> None:
    parser = fetch_static.build_parser()
    dest = tmp_path / "static"

    args = parser.parse_args(["--tag", "v1.2.3", "--repo", "acme/example", "--dest", str(dest)])

    assert args.repo == "acme/example"
    assert args.dest == dest


def test_fetch_static_calls_fetch_and_stage_then_swap_into_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "static"
    calls: list[tuple[str, Any]] = []

    def fake_fetch_and_stage(
        opener: Any,
        repo: str,
        tag: str,
        asset_name: str,
        user_agent: str,
        temp_dir: Path,
        *,
        not_found_message: str | None = None,
    ) -> None:
        calls.append(("fetch_and_stage", (repo, tag, asset_name, temp_dir)))
        assert temp_dir.parent == dest.parent
        assert asset_name == STATIC_ASSET_NAME_TEMPLATE.format(tag=tag)

    def fake_swap_into_place(temp_dir: Path, static_dir: Path) -> None:
        calls.append(("swap_into_place", (temp_dir, static_dir)))
        assert static_dir == dest

    monkeypatch.setattr(fetch_static, "fetch_and_stage", fake_fetch_and_stage)
    monkeypatch.setattr(fetch_static, "swap_into_place", fake_swap_into_place)

    fetch_static.fetch_static("v1.2.3", "acme/example", dest)

    assert [name for name, _ in calls] == ["fetch_and_stage", "swap_into_place"]
    assert calls[0][1][0] == "acme/example"
    assert calls[0][1][1] == "v1.2.3"


def test_main_prints_an_error_and_returns_1_when_fetch_static_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_fetch_static(tag: str, repo: str, dest: Path) -> None:
        raise StaticAssetError("checksum mismatch")

    monkeypatch.setattr(fetch_static, "fetch_static", failing_fetch_static)

    exit_code = fetch_static.main(["--tag", "v1.2.3", "--dest", str(tmp_path / "static")])

    assert exit_code == 1
    assert "checksum mismatch" in capsys.readouterr().err


def test_main_returns_0_and_reports_the_tag_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fetch_static, "fetch_static", lambda tag, repo, dest: None)

    exit_code = fetch_static.main(["--tag", "v1.2.3", "--dest", str(tmp_path / "static")])

    assert exit_code == 0
    assert "v1.2.3" in capsys.readouterr().out
