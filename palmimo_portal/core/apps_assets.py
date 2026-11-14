from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from palmimo_portal.core.manifest import Manifest, ManifestValidationError
from palmimo_portal.ports import (
    DisplayAssetMessage,
    DisplayBusyError,
    DisplayFirmwareNeed,
    DisplayToolError,
    RunLockTimeoutError,
)


if TYPE_CHECKING:
    from palmimo_portal.core.apps_jobs import AppsJobContext


ASSETS_LOCK_TIMEOUT_SECONDS = 300.0


class DisplayAssetsRequireV2Error(Exception):
    code = "display_assets_require_v2"

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(f"display assets require: {', '.join(missing)}")


class DisplayAssetConversionError(Exception):
    code = "display_asset_invalid"

    def __init__(self, name: str, errors: tuple[DisplayAssetMessage, ...]) -> None:
        self.name = name
        self.errors = errors
        details = "; ".join(f"{error.code} {error.path or ''}: {error.message}" for error in errors)
        super().__init__(f"asset {name}: {details}")


class DisplayCapacityError(DisplayToolError):
    code = "display_capacity_exceeded"

    def __init__(self, short_by: int) -> None:
        super().__init__(f"display assets exceed capacity by {short_by} bytes", short_by)


def stage_display_assets(
    ctx: AppsJobContext,
    manifest: Manifest,
    app_id: str,
    project_dir: Path,
    staging_dir: Path,
    *,
    had_assets: bool,
    on_step: Callable[[str], None],
) -> DisplayFirmwareNeed | None:
    need = ctx.app_environment.read_display_firmware_need(project_dir / ".venv") if ctx.app_environment else None
    if not manifest.assets and not had_assets:
        return need
    on_step("assets")
    if manifest.assets:
        missing = tuple(
            name
            for name, present in (
                ("display_firmware=2", need == "2"),
                ("motor_display", "motor_display" in manifest.devices),
            )
            if not present
        )
        if missing:
            raise DisplayAssetsRequireV2Error(missing)
    if ctx.asset_converter is None or ctx.platform is None or ctx.state is None:
        raise RuntimeError("display asset dependencies are unavailable")
    directory = staging_dir / ".display-assets"
    directory.mkdir(mode=0o750)
    sizes = []
    for name, asset in manifest.assets.items():
        conversion = ctx.asset_converter.convert(project_dir / asset.file, directory / f"{name}.bin")
        if not conversion.ok:
            raise DisplayAssetConversionError(name, conversion.errors)
        invalid_slots = set(asset.params) - conversion.params.keys()
        if invalid_slots or (asset.params and conversion.kind != "vector"):
            raise ManifestValidationError(
                [f"asset {name} has invalid slots: {sorted(invalid_slots or asset.params.keys())}"]
            )
        if conversion.bytes is None:
            raise ValueError(f"asset {name} has no converted byte count")
        sizes.append(((conversion.bytes + 4095) // 4096) * 4096)
    try:
        with ctx.state.lock_run(timeout_s=ASSETS_LOCK_TIMEOUT_SECONDS):
            status = ctx.platform.display_assets_status()
            occupied = [*sizes, *(((asset.bytes + 4095) // 4096) * 4096 for asset in status.standard)]
            short_by = sum(occupied) + max(occupied, default=0) - status.capacity
            if short_by > 0:
                raise DisplayCapacityError(short_by)
            try:
                ctx.platform.replace_display_assets(app_id, directory)
            except DisplayToolError as error:
                if error.short_by is not None:
                    raise DisplayCapacityError(error.short_by) from error
                raise
    except RunLockTimeoutError as error:
        raise DisplayBusyError("display asset synchronization is busy") from error
    # The staging container itself can become the installed tree.
    shutil.rmtree(directory)
    return need
