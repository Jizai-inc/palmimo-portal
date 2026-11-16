from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_portal.adapters.display_previews import DisplayAssetPreviews
from palmimo_portal.testing.fakes import FakeClockPort, FakeDisplayAssetConverterPort


def test_preview_expiry_removes_file_but_waits_for_active_save(tmp_path: Path) -> None:
    clock = FakeClockPort()
    store = DisplayAssetPreviews(tmp_path / "previews", FakeDisplayAssetConverterPort(), clock.monotonic)
    preview = store.create("happy", b"source", ".json")

    with store.open("happy", preview.token) as opened:
        assert opened is not None
        clock.monotonic_value = 600
        store.delete(preview.token)
        assert preview.path.is_file()
    assert not preview.path.exists()
    with store.open("happy", preview.token) as opened:
        assert opened is None


def test_restarting_preview_store_invalidates_old_tokens_and_removes_files(tmp_path: Path) -> None:
    directory = tmp_path / "previews"
    store = DisplayAssetPreviews(directory, FakeDisplayAssetConverterPort())
    preview = store.create("happy", b"source", ".json")

    restarted = DisplayAssetPreviews(directory, FakeDisplayAssetConverterPort())

    assert not preview.path.exists()
    with restarted.open("happy", preview.token) as opened:
        assert opened is None
    store.delete(preview.token)


def test_saved_original_survives_restart_while_temporary_sources_are_removed(tmp_path: Path) -> None:
    directory = tmp_path / "display-assets" / "previews"
    store = DisplayAssetPreviews(directory, FakeDisplayAssetConverterPort())
    preview = store.create("happy", b"original", ".json")
    with store.save_original("happy", preview):
        pass

    restarted = DisplayAssetPreviews(directory, FakeDisplayAssetConverterPort())

    assert restarted.read_original("happy") == (b"original", "application/json")
    assert not preview.source_path.exists()
    assert not preview.path.exists()


@pytest.mark.parametrize("condition", ["symlink", "oversized"])
def test_saved_original_refuses_unsafe_files(tmp_path: Path, condition: str) -> None:
    store = DisplayAssetPreviews(tmp_path / "display-assets" / "previews", FakeDisplayAssetConverterPort())
    store.originals.mkdir()
    path = store.originals / "happy.png"
    if condition == "symlink":
        outside = tmp_path / "private"
        outside.write_bytes(b"private")
        path.symlink_to(outside)
    else:
        path.write_bytes(b"x" * (16 * 1024 * 1024 + 1))

    assert store.read_original("happy") is None
