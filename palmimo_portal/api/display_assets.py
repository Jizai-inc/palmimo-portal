from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import PurePath
from typing import Literal

import anyio.to_thread
from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel

from palmimo_portal.api.deps import require_auth, require_full_session, require_provisioned
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core.apps_assets import DisplayAssetConversionError, DisplayCapacityError
from palmimo_portal.core.display_assets import (
    DisplayAssetCapacity,
    DisplayAssetsService,
    DisplayPreviewExpiredError,
    UnknownDisplayAssetError,
)
from palmimo_portal.ports import (
    AppsLockTimeoutError,
    DisplayAssetMessage,
    DisplayBusyError,
    DisplaySyncPort,
    DisplaySyncResult,
    DisplayToolError,
    RunLockTimeoutError,
)


logger = logging.getLogger("palmimo_portal")


def require_platform_ready(request: Request) -> None:
    if not request.app.state.start_deps.platform_ready():
        raise PortalError(409, "platform_not_ready")


router = APIRouter(
    prefix="/api/v1/display-assets",
    tags=["display-assets"],
    dependencies=[
        Depends(require_provisioned),
        Depends(require_auth),
        Depends(require_full_session),
        Depends(require_platform_ready),
    ],
)
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


class StandardAssetInfo(BaseModel):
    name: str
    description: str | None
    translations: dict[str, dict[str, str]]
    overridden: bool
    kind: Literal["vector", "image", "gif"]
    default_kind: Literal["vector", "image", "gif"]
    bytes: int
    default_bytes: int
    params: list[str]
    default_params: list[str]


class StandardAssetsResponse(BaseModel):
    standard: list[StandardAssetInfo]
    orphan_overrides: list[str]


class DisplayAssetPreviewResponse(BaseModel):
    token: str
    kind: Literal["vector", "image", "gif"]
    bytes: int
    crc32: str
    warnings: list[DisplayAssetMessage]
    params_lost: list[str]
    capacity: DisplayAssetCapacity


class DisplayAssetPutRequest(BaseModel):
    token: str


class DisplayAssetSavedResponse(BaseModel):
    sync: DisplaySyncResult


def get_display_assets(request: Request) -> DisplayAssetsService:
    adapters = request.app.state.adapters
    return DisplayAssetsService(adapters.state, adapters.platform, adapters.standard_assets, adapters.display_previews)


def get_display_sync(request: Request) -> DisplaySyncPort:
    service: DisplaySyncPort | None = getattr(request.app.state, "display_sync", None)
    if service is None:
        service = request.app.state.adapters.display_sync
    if service is None:
        raise PortalError(503, "system_backend_unavailable")
    return service


@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    except UnknownDisplayAssetError as error:
        raise PortalError(404, "display_asset_unknown") from error
    except DisplayPreviewExpiredError as error:
        raise PortalError(409, "display_asset_preview_expired") from error
    except DisplayCapacityError as error:
        raise PortalError(409, "display_capacity_exceeded", short_by=error.short_by) from error
    except DisplayAssetConversionError as error:
        raise PortalError(422, "display_asset_invalid", errors=[asdict(entry) for entry in error.errors]) from error
    except (AppsLockTimeoutError, RunLockTimeoutError) as error:
        raise PortalError(409, "app_busy") from error
    except DisplayBusyError as error:
        raise PortalError(409, "display_busy") from error
    except DisplayToolError as error:
        logger.error("display asset tool failed: %s", error.reason)
        raise PortalError(500, "internal_error") from error


@router.get("/standard")
def list_standard_assets(service: DisplayAssetsService = Depends(get_display_assets)) -> StandardAssetsResponse:
    """Include uncatalogued system assets and orphan overrides; translations are selected by the client."""
    with _translate_errors():
        status, catalog = service.listing()
    return StandardAssetsResponse(
        standard=[
            StandardAssetInfo(
                **asdict(row),
                description=catalog.get(row.name, {}).get("description"),
                translations=catalog.get(row.name, {}).get("translations", {}),
            )
            for row in status.standard
        ],
        orphan_overrides=list(status.orphan_overrides),
    )


@router.post("/standard/{name}/preview")
async def preview_standard_asset(
    name: str,
    request: Request,
    file: UploadFile = File(...),
    service: DisplayAssetsService = Depends(get_display_assets),
) -> DisplayAssetPreviewResponse:
    """Accept up to 16 MiB; preview tokens are bound to the asset name and expire after ten minutes."""
    with _translate_errors():
        service.require_standard(name)
        form = await request.form()
        if sum(hasattr(value, "read") for _, value in form.multi_items()) != 1:
            raise PortalError(422, "display_asset_invalid")
        if file.size is not None and file.size > MAX_UPLOAD_BYTES:
            raise PortalError(422, "display_asset_invalid")
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > MAX_UPLOAD_BYTES:
                raise PortalError(422, "display_asset_invalid")
        preview, lost, capacity = await anyio.to_thread.run_sync(
            service.preview, name, bytes(content), PurePath(file.filename or "").suffix
        )
    conversion = preview.conversion
    assert conversion.kind is not None and conversion.bytes is not None and conversion.crc32 is not None
    return DisplayAssetPreviewResponse(
        token=preview.token,
        kind=conversion.kind,
        bytes=conversion.bytes,
        crc32=conversion.crc32,
        warnings=list(conversion.warnings),
        params_lost=lost,
        capacity=capacity,
    )


@router.put("/standard/{name}")
def put_standard_asset(
    name: str,
    body: DisplayAssetPutRequest,
    request: Request,
    service: DisplayAssetsService = Depends(get_display_assets),
) -> DisplayAssetSavedResponse:
    """Recheck capacity before saving; a busy sync leaves the override saved for a later retry."""
    with _translate_errors():
        service.require_standard(name)
        return DisplayAssetSavedResponse(sync=service.put(name, body.token, get_display_sync(request)))


@router.delete("/standard/{name}")
def delete_standard_asset(
    name: str,
    service: DisplayAssetsService = Depends(get_display_assets),
    sync: DisplaySyncPort = Depends(get_display_sync),
) -> DisplayAssetSavedResponse:
    """Reset standard overrides, including names removed from the current bundle."""
    with _translate_errors():
        return DisplayAssetSavedResponse(sync=service.delete(name, sync))


@router.post("/sync")
def sync_display_assets(
    service: DisplayAssetsService = Depends(get_display_assets),
    sync: DisplaySyncPort = Depends(get_display_sync),
) -> DisplaySyncResult:
    """Wait for synchronization, up to the tool's 300-second budget."""
    with _translate_errors():
        return service.sync(sync)
