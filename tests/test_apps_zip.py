"""Behavioral tests for safe zip extraction (design doc 3.4's "extract" row)."""

from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path

import pytest

from palmimo_portal.core.apps_zip import extract_zip_to_staging
from palmimo_portal.ports import InvalidManifestSourceError


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


_VALID_APP = {"palmimo.toml": 'schema = 1\nname = "app"\n', "pyproject.toml": "[project]\nname='app'\n"}


def test_extract_zip_to_staging_finds_manifest_at_depth_zero(tmp_path: Path) -> None:
    dest = tmp_path / "staged"
    app_root = extract_zip_to_staging(_zip_bytes(_VALID_APP), dest)
    assert app_root == dest
    assert (app_root / "palmimo.toml").is_file()


def test_extract_zip_to_staging_peels_single_top_level_directory(tmp_path: Path) -> None:
    wrapped = {f"myapp-1.0/{name}": content for name, content in _VALID_APP.items()}
    dest = tmp_path / "staged"
    app_root = extract_zip_to_staging(_zip_bytes(wrapped), dest)
    assert app_root == dest / "myapp-1.0"
    assert (app_root / "palmimo.toml").is_file()


def test_extract_zip_to_staging_preserves_the_executable_bit(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in _VALID_APP.items():
            archive.writestr(name, content)
        info = zipfile.ZipInfo("run.sh")
        info.external_attr = 0o755 << 16
        archive.writestr(info, "#!/bin/sh\necho hi\n")

    app_root = extract_zip_to_staging(buffer.getvalue(), tmp_path / "staged")

    assert os.access(app_root / "run.sh", os.X_OK)


def test_extract_zip_to_staging_strips_setuid_from_the_extracted_mode(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in _VALID_APP.items():
            archive.writestr(name, content)
        info = zipfile.ZipInfo("run.sh")
        info.external_attr = (stat.S_ISUID | 0o755) << 16
        archive.writestr(info, "#!/bin/sh\necho hi\n")

    app_root = extract_zip_to_staging(buffer.getvalue(), tmp_path / "staged")

    assert not (app_root / "run.sh").stat().st_mode & stat.S_ISUID


def test_extract_zip_to_staging_rejects_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(InvalidManifestSourceError):
        extract_zip_to_staging(_zip_bytes({"README.md": "hi"}), tmp_path / "staged")


def test_extract_zip_to_staging_rejects_multiple_manifests_at_depth_one(tmp_path: Path) -> None:
    entries = {f"a/{k}": v for k, v in _VALID_APP.items()} | {f"b/{k}": v for k, v in _VALID_APP.items()}
    with pytest.raises(InvalidManifestSourceError):
        extract_zip_to_staging(_zip_bytes(entries), tmp_path / "staged")


def test_extract_zip_to_staging_rejects_absolute_path_entry(tmp_path: Path) -> None:
    entries = dict(_VALID_APP)
    entries["/etc/passwd"] = "pwned"
    with pytest.raises(InvalidManifestSourceError):
        extract_zip_to_staging(_zip_bytes(entries), tmp_path / "staged")


def test_extract_zip_to_staging_rejects_parent_traversal_entry(tmp_path: Path) -> None:
    entries = dict(_VALID_APP)
    entries["../escape.txt"] = "pwned"
    with pytest.raises(InvalidManifestSourceError):
        extract_zip_to_staging(_zip_bytes(entries), tmp_path / "staged")


def test_extract_zip_to_staging_rejects_backslash_separated_entry(tmp_path: Path) -> None:
    entries = dict(_VALID_APP)
    entries["subdir\\escape.txt"] = "pwned"
    with pytest.raises(InvalidManifestSourceError):
        extract_zip_to_staging(_zip_bytes(entries), tmp_path / "staged")


def test_extract_zip_to_staging_rejects_symlink_entry(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in _VALID_APP.items():
            archive.writestr(name, content)
        link_info = zipfile.ZipInfo("evil_link")
        link_info.create_system = 3  # unix
        link_info.external_attr = 0o120777 << 16  # S_IFLNK
        archive.writestr(link_info, "/etc/passwd")

    with pytest.raises(InvalidManifestSourceError):
        extract_zip_to_staging(buffer.getvalue(), tmp_path / "staged")


def test_extract_zip_to_staging_rejects_duplicate_entry(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("palmimo.toml", _VALID_APP["palmimo.toml"])
        archive.writestr("palmimo.toml", _VALID_APP["palmimo.toml"])
        archive.writestr("pyproject.toml", _VALID_APP["pyproject.toml"])

    with pytest.raises(InvalidManifestSourceError):
        extract_zip_to_staging(buffer.getvalue(), tmp_path / "staged")


def test_extract_zip_to_staging_removes_partial_output_on_failure(tmp_path: Path) -> None:
    dest = tmp_path / "staged"
    entries = dict(_VALID_APP)
    entries["../escape.txt"] = "pwned"
    with pytest.raises(InvalidManifestSourceError):
        extract_zip_to_staging(_zip_bytes(entries), dest)
    assert not dest.exists()
