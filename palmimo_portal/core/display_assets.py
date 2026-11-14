from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from palmimo_portal.core.apps_assets import DisplayAssetConversionError, DisplayCapacityError
from palmimo_portal.ports import (
    DisplayAssetPreview,
    DisplayAssetPreviewsPort,
    DisplayAssetsStatus,
    DisplaySyncPort,
    DisplaySyncResult,
    PlatformPort,
    StandardAssetsPort,
    StateStore,
)


class UnknownDisplayAssetError(Exception):
    pass


class DisplayPreviewExpiredError(Exception):
    pass


@dataclass(frozen=True)
class DisplayAssetCapacity:
    ok: bool
    short_by: int
    apps_no_longer_fit: tuple[str, ...]


def replacement_capacity(status: DisplayAssetsStatus, name: str, size: int) -> DisplayAssetCapacity:
    sizes = [((size if row.name == name else row.bytes) + 4095) // 4096 * 4096 for row in status.standard]
    total = sum(sizes)
    largest = max(sizes, default=0)
    short_by = max(0, total + largest - status.capacity)
    apps = []
    for app_id, app in sorted(status.apps.items()):
        shortage = total + app.bytes + max(largest, app.largest) - status.capacity
        if shortage > 0:
            apps.append(app_id)
            short_by = max(short_by, shortage)
    return DisplayAssetCapacity(short_by == 0, short_by, tuple(apps))


@dataclass
class DisplayAssetsService:
    state: StateStore
    platform: PlatformPort
    standards: StandardAssetsPort
    previews: DisplayAssetPreviewsPort

    def require_standard(self, name: str) -> None:
        if name not in self.standards.list_standard_names():
            raise UnknownDisplayAssetError(name)

    def listing(self) -> tuple[DisplayAssetsStatus, dict[str, Any]]:
        with self.state.lock_run():
            status = self.platform.display_assets_status()
        catalog = self.standards.read_catalog()
        entries = {entry["id"].removeprefix("palmimo/"): entry for entry in catalog.get("assets", [])}
        return status, entries

    def preview(
        self, name: str, content: bytes, suffix: str
    ) -> tuple[DisplayAssetPreview, list[str], DisplayAssetCapacity]:
        self.require_standard(name)
        preview = self.previews.create(name, content, suffix)
        try:
            conversion = preview.conversion
            if not conversion.ok:
                raise DisplayAssetConversionError(name, conversion.errors)
            with self.state.lock_run():
                status = self.platform.display_assets_status()
            standard = next(row for row in status.standard if row.name == name)
            lost = sorted(set(standard.default_params) - conversion.params.keys())
            if conversion.bytes is None:
                raise ValueError("converted asset has no size")
            return preview, lost, replacement_capacity(status, name, conversion.bytes)
        except BaseException:
            self.previews.delete(preview.token)
            raise

    def put(self, name: str, token: str, sync: DisplaySyncPort) -> DisplaySyncResult:
        self.require_standard(name)
        with self.state.lock_apps(), self.state.lock_run(), self.previews.open(name, token) as preview:
            if preview is None:
                raise DisplayPreviewExpiredError(token)
            status = self.platform.display_assets_status()
            if preview.conversion.bytes is None:
                raise ValueError("converted asset has no size")
            capacity = replacement_capacity(status, name, preview.conversion.bytes)
            if not capacity.ok:
                raise DisplayCapacityError(capacity.short_by)
            self.platform.override_display_asset(name, preview.path)
            result = sync.sync()
        self.previews.delete(token)
        return result

    def delete(self, name: str, sync: DisplaySyncPort) -> DisplaySyncResult:
        names = self.standards.list_standard_names()
        with self.state.lock_apps(), self.state.lock_run():
            if name not in names and name not in self.platform.display_assets_status().orphan_overrides:
                raise UnknownDisplayAssetError(name)
            self.platform.override_display_asset(name, None)
            return sync.sync()

    def sync(self, sync: DisplaySyncPort) -> DisplaySyncResult:
        with self.state.lock_apps(), self.state.lock_run():
            return sync.sync()
