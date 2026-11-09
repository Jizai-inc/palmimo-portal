from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from palmimo_portal.api.apps import AppSourceInfo, EnvSpecInfo
from palmimo_portal.api.deps import require_auth, require_full_session, require_provisioned
from palmimo_portal.core.public_catalog import PublicCatalogCache, public_page_url
from palmimo_portal.ports import PublicAppIdentity


router = APIRouter(
    prefix="/api/v1/catalog/public",
    tags=["catalog"],
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)


class PublicCatalogAppInfo(BaseModel):
    name: str
    description: str
    translations: dict[str, dict[str, str]]
    source: AppSourceInfo
    env: list[EnvSpecInfo]
    devices: list[str]
    public: PublicAppIdentity
    public_page_url: str | None


class PublicCatalogResponse(BaseModel):
    generated_at: str | None
    apps: list[PublicCatalogAppInfo]
    fetched_at: float | None
    stale: bool
    reason: str | None


@router.get("")
def get_public_catalog(request: Request) -> PublicCatalogResponse:
    """Return the public catalog, refreshing an unfetched or expired cache before responding."""
    cache: PublicCatalogCache = request.app.state.public_catalog_cache
    snapshot = cache.get(ntp_synchronized=request.app.state.adapters.clock.ntp_synchronized())
    return PublicCatalogResponse(
        generated_at=snapshot.generated_at,
        apps=[
            PublicCatalogAppInfo(
                name=app.name,
                description=app.description,
                translations=app.translations,
                source=AppSourceInfo(
                    type=app.source.type,
                    url=app.source.url,
                    ref=app.source.ref,
                    ref_kind=app.source.ref_kind,
                    subdir=app.source.subdir,
                    commit=app.source.commit,
                    manifest=app.source.manifest,
                    official=app.public.official,
                ),
                env=[
                    EnvSpecInfo(name=name, required=spec.required, description=spec.description, help_url=spec.help_url)
                    for name, spec in sorted(app.env.items())
                ],
                devices=list(app.devices),
                public=app.public,
                public_page_url=public_page_url(
                    request.app.state.apps_job_context.public_catalog_url, app.public.app_id
                ),
            )
            for app in snapshot.apps
        ],
        fetched_at=snapshot.fetched_at,
        stale=snapshot.stale,
        reason=snapshot.reason,
    )
