"""Tests for the atomic-write helpers: durability (fsync) and exclusive creation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from palmimo_portal.adapters.atomic_write import atomic_write_text, create_exclusive_text


def test_atomic_write_text_fsyncs_the_file_and_the_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced_fds: list[int] = []
    original_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsynced_fds.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    atomic_write_text(tmp_path / "auth.json", "hello")

    # One fsync for the temp file's own fd, one for the directory fd opened
    # after the rename -- both must run so a power loss right after this
    # call cannot lose or empty the file despite the rename having "completed".
    assert len(fsynced_fds) == 2


def test_atomic_write_text_still_round_trips_after_fsync_is_added(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"

    atomic_write_text(path, "hello")

    assert path.read_text(encoding="utf-8") == "hello"


def test_create_exclusive_text_creates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"

    create_exclusive_text(path, "hello")

    assert path.read_text(encoding="utf-8") == "hello"


def test_create_exclusive_text_raises_when_the_file_already_exists(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    create_exclusive_text(path, "first")

    with pytest.raises(FileExistsError):
        create_exclusive_text(path, "second")

    # The loser must not have clobbered the winner's content.
    assert path.read_text(encoding="utf-8") == "first"


def test_create_exclusive_text_fsyncs_the_file_and_the_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced_fds: list[int] = []
    original_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsynced_fds.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    create_exclusive_text(tmp_path / "auth.json", "hello")

    assert len(fsynced_fds) == 2


def test_create_exclusive_text_sets_mode_0600(tmp_path: Path) -> None:
    import stat

    path = tmp_path / "auth.json"

    create_exclusive_text(path, "hello")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
