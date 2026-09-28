"""The official app catalog cache (design doc 4.1): devkit's latest examples release, 1-hour cached.

:class:`CatalogCache` mirrors :class:`~palmimo_portal.core.platform.PlatformLatestCache`'s
TTL shape, with two differences the catalog's own contract needs: a
persisted last-good copy (``GET /catalog`` must never look "empty" just
because GitHub is briefly unreachable) and an explicit NTP gate (a fetch
attempted before the clock syncs would just fail on TLS, so it is skipped
and reported as ``reason: "clock_unsynced"`` instead).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from palmimo_portal.ports import CatalogApp, CatalogCacheState, CatalogSource, CatalogSourceError, StateStore


logger = logging.getLogger("palmimo_portal")

#: How long a successful fetch is served before the next `get()` refreshes again (design doc 4.1's
#: 1-hour cache).
CATALOG_TTL_SECONDS = 3600.0

#: How long a failed refresh (network error, rate limit, an invalid asset) is left alone before
#: the next `get()` retries -- keeps a GitHub outage or rate limit from turning into one refetch
#: attempt per request.
CATALOG_BACKOFF_SECONDS = 300.0


class CatalogSnapshot:
    """What `GET /catalog` reports -- the answer to one `CatalogCache.get()`/`refresh()` call."""

    __slots__ = ("apps", "fetched_at", "reason", "stale", "tag")

    def __init__(
        self,
        *,
        tag: str | None,
        apps: tuple[CatalogApp, ...],
        fetched_at: float | None,
        stale: bool,
        reason: str | None,
    ) -> None:
        self.tag = tag
        self.apps = apps
        self.fetched_at = fetched_at
        self.stale = stale
        self.reason = reason


class CatalogCache:
    """In-process cache of the official catalog, backed by a persisted last-good copy.

    Not thread-safe against overlapping refreshes (a lost update at worst
    duplicates one GitHub round trip) -- the same tradeoff
    :class:`~palmimo_portal.core.platform.PlatformLatestCache` makes.
    """

    def __init__(
        self,
        source: CatalogSource,
        state_store: StateStore,
        *,
        ttl_seconds: float = CATALOG_TTL_SECONDS,
        backoff_seconds: float = CATALOG_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._state_store = state_store
        self._ttl_seconds = ttl_seconds
        self._backoff_seconds = backoff_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._fetched_at_monotonic: float | None = None
        self._fetched_at_wall: float | None = None
        self._failed_at_monotonic: float | None = None
        self._tag: str | None = None
        self._apps: tuple[CatalogApp, ...] = ()
        self._last_reason: str | None = None

    def _fresh(self) -> bool:
        return (
            self._fetched_at_monotonic is not None
            and self._monotonic() - self._fetched_at_monotonic < self._ttl_seconds
        )

    def _recently_attempted(self) -> bool:
        if self._fresh():
            return True
        return (
            self._failed_at_monotonic is not None
            and self._monotonic() - self._failed_at_monotonic < self._backoff_seconds
        )

    def peek(self) -> CatalogSnapshot:
        """Return whatever the cache currently holds, without triggering a fetch.

        Used by a request path (:func:`~palmimo_portal.core.apps_jobs.check_git_update`)
        that wants the latest known release tag but must not itself start a
        GitHub round trip -- the periodic task (design doc 4.1's 1-hour
        cadence) is the only thing that refreshes this cache.
        """
        return self._snapshot()

    def get(self, *, ntp_synchronized: bool) -> CatalogSnapshot:
        """Return the cached catalog, refreshing first unless the in-memory copy is fresh or a recent attempt failed."""
        if not self._recently_attempted():
            self.refresh(ntp_synchronized=ntp_synchronized)
        return self._snapshot()

    def refresh(self, *, ntp_synchronized: bool) -> None:
        """Unconditionally attempt a fetch -- the periodic task's entry point.

        A failure (network, checksum, invalid asset shape) or an
        unsynchronized clock leaves the previous in-memory/persisted state
        untouched; only :attr:`CatalogSnapshot.reason` records why the
        latest attempt did not refresh anything.
        """
        if not ntp_synchronized:
            logger.info("catalog: skipped reason=clock_unsynced")
            self._last_reason = "clock_unsynced"
            return
        try:
            asset = self._source.fetch()
        except CatalogSourceError as error:
            reason = "rate_limited" if error.code == "rate_limited" else "offline"
            logger.warning("catalog: stale reason=%s: %s", reason, error)
            self._last_reason = reason
            self._failed_at_monotonic = self._monotonic()
            return
        self._tag = asset.tag
        self._apps = asset.apps
        self._fetched_at_monotonic = self._monotonic()
        self._fetched_at_wall = self._wall_clock()
        self._failed_at_monotonic = None
        self._last_reason = None
        self._state_store.write_catalog_cache(
            CatalogCacheState(tag=asset.tag, apps=asset.apps, fetched_at=self._fetched_at_wall)
        )
        logger.info("catalog: fetched tag=%s", asset.tag)

    def _snapshot(self) -> CatalogSnapshot:
        if self._fresh():
            return CatalogSnapshot(
                tag=self._tag, apps=self._apps, fetched_at=self._fetched_at_wall, stale=False, reason=None
            )
        if self._tag is not None:
            return CatalogSnapshot(
                tag=self._tag,
                apps=self._apps,
                fetched_at=self._fetched_at_wall,
                stale=True,
                reason=self._last_reason or "offline",
            )
        persisted = self._state_store.read_catalog_cache()
        return CatalogSnapshot(
            tag=persisted.tag,
            apps=persisted.apps,
            fetched_at=persisted.fetched_at,
            stale=True,
            reason=self._last_reason if persisted.tag is None else (self._last_reason or "offline"),
        )
