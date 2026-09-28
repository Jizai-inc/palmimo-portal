"""``/api/v1/catalog``: the official app catalog (design doc 4.1).

Serves whatever :class:`~palmimo_portal.core.catalog.CatalogCache` currently
holds -- a periodic background task (:mod:`palmimo_portal.core.periodic`)
keeps it fresh; this endpoint refreshes it lazily too, so a device whose
periodic thread has not caught up yet (or runs with ``periodic_enabled =
False``, as tests do) still gets an up-to-date answer on first request.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from palmimo_portal.api.apps import AppSourceInfo, EnvSpecInfo
from palmimo_portal.api.deps import require_auth, require_full_session, require_provisioned


router = APIRouter(
    prefix="/api/v1/catalog",
    tags=["catalog"],
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)


class CatalogAppInfo(BaseModel):
    name: str
    description: str
    source: AppSourceInfo
    env: list[EnvSpecInfo]
    devices: list[str]


class CatalogResponse(BaseModel):
    tag: str | None
    apps: list[CatalogAppInfo]
    fetched_at: float | None
    stale: bool
    reason: str | None


@router.get("")
def get_catalog(request: Request) -> CatalogResponse:
    """Report the official app catalog, refreshing it first if the cached copy is stale.

    ``stale`` is true whenever the answer is not a fresh (< 1 hour old)
    fetch -- either the last-known-good copy after a failed refresh
    (``reason: "offline"``), or nothing has ever been fetched and the
    clock is not yet NTP-synchronized (``reason: "clock_unsynced"``, design
    doc 3.6 -- a fetch attempted before sync would just fail on TLS).
    """
    cache = request.app.state.catalog_cache
    clock = request.app.state.adapters.clock
    snapshot = cache.get(ntp_synchronized=clock.ntp_synchronized())
    return CatalogResponse(
        tag=snapshot.tag,
        apps=[
            CatalogAppInfo(
                name=a.name,
                description=a.description,
                source=AppSourceInfo(
                    type=a.source.type,
                    url=a.source.url,
                    ref=a.source.ref,
                    ref_kind=a.source.ref_kind,
                    subdir=a.source.subdir,
                    commit=a.source.commit,
                    manifest=a.source.manifest,
                    #: Every catalog entry is generated from the official devkit repo itself
                    #: (design doc 4.1) -- there is no non-official catalog source.
                    official=True,
                ),
                env=[
                    EnvSpecInfo(name=name, required=spec.required, description=spec.description, help_url=spec.help_url)
                    for name, spec in sorted(a.env.items())
                ],
                devices=list(a.devices),
            )
            for a in snapshot.apps
        ],
        fetched_at=snapshot.fetched_at,
        stale=snapshot.stale,
        reason=snapshot.reason,
    )
