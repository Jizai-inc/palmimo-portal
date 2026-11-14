from __future__ import annotations

from pathlib import Path

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
