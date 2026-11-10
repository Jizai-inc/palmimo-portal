from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import quote, urlsplit, urlunsplit

from palmimo_portal.core.legacy_official import legacy_official
from palmimo_portal.ports import (
    AppRecord,
    AppSource,
    AppsState,
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
        except PublicCatalogSourceError as error:
            logger.warning("public catalog: keeping previous cache: %s", error)
            self._reason = "offline"
            self._failed_at_monotonic = self._monotonic()
            return
        self._state = state
        self._fetched_at_monotonic = self._monotonic()
        self._failed_at_monotonic = None
        self._reason = None
        try:
            self._state_store.write_public_catalog_cache(state)
        except OSError as error:
            self._reason = "persistence_failed"
            logger.warning("public catalog: fetched but persistence failed: %s", error)


class PublicAppChangedError(Exception):
    pass


class PublicAppMismatchError(Exception):
    pass


class PublicAppUnavailableError(Exception):
    pass


def find_public_app(cache: PublicCatalogCache | None, public_app_id: str) -> PublicCatalogApp | None:
    if cache is None:
        return None
    return next((app for app in cache.peek().apps if app.public.app_id == public_app_id), None)


def is_public_source(record: AppRecord) -> bool:
    return bool(record.source.public_app_id) or legacy_official(record) is not None


def effective_public_source(cache: PublicCatalogCache | None, record: AppRecord) -> AppSource:
    legacy = legacy_official(record)
    source = record.source
    if legacy is None:
        return source
    candidate = (
        next(
            (
                app
                for app in cache.peek().apps
                if app.public.official and app.public.namespace == "palmimo" and app.name == legacy.name
            ),
            None,
        )
        if cache is not None and legacy.name is not None
        else None
    )
    return replace(source, public_app_id=candidate.public.app_id if candidate else None)


def installed_public_app(state: AppsState, cache: PublicCatalogCache | None, public_app_id: str) -> str | None:
    return next(
        (
            record.id
            for record in state.apps.values()
            if effective_public_source(cache, record).public_app_id == public_app_id
        ),
        None,
    )


class PublicAppInstalledError(Exception):
    def __init__(self, app_id: str) -> None:
        self.app_id = app_id
        super().__init__(app_id)


@dataclass(frozen=True)
class PublicAppStatus:
    available: bool | None
    update_available: bool | None
    latest_commit: str | None
    official: bool | None


def public_app_status(cache: PublicCatalogCache | None, record: AppRecord) -> PublicAppStatus:
    legacy = legacy_official(record)
    if legacy is not None and legacy.name is None:
        return PublicAppStatus(False, False, None, True)
    source = effective_public_source(cache, record)
    if cache is None or cache.peek().generated_at is None:
        return PublicAppStatus(None, None, None, True if legacy else None)
    app = find_public_app(cache, source.public_app_id or "")
    if app is None:
        return PublicAppStatus(False, False, None, bool(legacy))
    return PublicAppStatus(True, source.commit != app.source.commit, app.source.commit, app.public.official)


def public_update_source(cache: PublicCatalogCache | None, record: AppRecord) -> AppSource:
    source = effective_public_source(cache, record)
    app = find_public_app(cache, source.public_app_id or "")
    if app is None:
        raise PublicAppUnavailableError("public_app_unavailable")
    return replace(app.source, public_app_id=source.public_app_id)


def public_page_url(catalog_url: str | None, public_app_id: str) -> str | None:
    if catalog_url is None:
        return None
    try:
        origin = urlsplit(catalog_url)
        if origin.scheme not in ("http", "https") or not origin.netloc:
            return None
        return urlunsplit((origin.scheme, origin.netloc, f"/apps/{quote(public_app_id, safe='')}", "", ""))
    except ValueError:
        return None
