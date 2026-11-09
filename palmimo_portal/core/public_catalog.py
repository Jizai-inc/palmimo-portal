from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from palmimo_portal.ports import (
    AppSource,
    PublicCatalogApp,
    PublicCatalogCacheState,
    PublicCatalogSource,
    PublicCatalogSourceError,
    StateStore,
)


logger = logging.getLogger("palmimo_portal")
PUBLIC_CATALOG_TTL_SECONDS = 3600.0
PUBLIC_CATALOG_BACKOFF_SECONDS = 300.0


@dataclass(frozen=True)
class PublicCatalogSnapshot:
    generated_at: str | None
    apps: tuple[PublicCatalogApp, ...]
    fetched_at: float | None
    stale: bool
    reason: str | None


class PublicCatalogCache:
    """Keep the last successful catalog; persisted wall times are display-only."""

    def __init__(
        self,
        source: PublicCatalogSource,
        state_store: StateStore,
        *,
        ttl_seconds: float = PUBLIC_CATALOG_TTL_SECONDS,
        backoff_seconds: float = PUBLIC_CATALOG_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._state_store = state_store
        self._state = state_store.read_public_catalog_cache()
        self._ttl_seconds = ttl_seconds
        self._backoff_seconds = backoff_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._fetched_at_monotonic: float | None = None
        self._failed_at_monotonic: float | None = None
        self._reason: str | None = None

    def _fresh(self) -> bool:
        return (
            self._fetched_at_monotonic is not None
            and self._monotonic() - self._fetched_at_monotonic < self._ttl_seconds
        )

    def peek(self) -> PublicCatalogSnapshot:
        stale = not self._fresh() or self._reason is not None
        return PublicCatalogSnapshot(
            self._state.generated_at,
            self._state.apps,
            self._state.fetched_at,
            stale,
            self._reason,
        )

    def get(self, *, ntp_synchronized: bool) -> PublicCatalogSnapshot:
        recently_failed = (
            self._failed_at_monotonic is not None
            and self._monotonic() - self._failed_at_monotonic < self._backoff_seconds
        )
        if not self._fresh() and not recently_failed:
            self.refresh(ntp_synchronized=ntp_synchronized)
        return self.peek()

    def refresh(self, *, ntp_synchronized: bool) -> None:
        """Unconditionally refresh for the hourly scheduler, once NTP is synchronized."""
        if not ntp_synchronized:
            self._reason = "clock_unsynced"
            return
        try:
            asset = self._source.fetch()
            state = PublicCatalogCacheState(asset.generated_at, asset.apps, self._wall_clock())
            self._state_store.write_public_catalog_cache(state)
        except (PublicCatalogSourceError, OSError) as error:
            logger.warning("public catalog: keeping previous cache: %s", error)
            self._reason = "offline"
            self._failed_at_monotonic = self._monotonic()
            return
        self._state = state
        self._fetched_at_monotonic = self._monotonic()
        self._failed_at_monotonic = None
        self._reason = None


class PublicAppUnavailableError(Exception):
    pass


def find_public_app(cache: PublicCatalogCache | None, public_app_id: str) -> PublicCatalogApp | None:
    if cache is None:
        return None
    return next((app for app in cache.peek().apps if app.public.app_id == public_app_id), None)


@dataclass(frozen=True)
class PublicAppStatus:
    available: bool
    update_available: bool
    latest_commit: str | None
    official: bool


def public_app_status(cache: PublicCatalogCache | None, source: AppSource) -> PublicAppStatus:
    app = find_public_app(cache, source.public_app_id or "")
    if app is None:
        return PublicAppStatus(False, False, None, False)
    return PublicAppStatus(True, source.commit != app.source.commit, app.source.commit, app.public.official)


def public_update_source(cache: PublicCatalogCache | None, source: AppSource) -> AppSource:
    app = find_public_app(cache, source.public_app_id or "")
    if app is None:
        raise PublicAppUnavailableError("public_app_unavailable")
    return replace(app.source, public_app_id=source.public_app_id)
