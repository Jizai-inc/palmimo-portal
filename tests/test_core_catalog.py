"""Behavioral tests for :class:`~palmimo_portal.core.catalog.CatalogCache` (design doc 4.1/3.6)."""

from __future__ import annotations

from palmimo_portal.core.catalog import CatalogCache
from palmimo_portal.ports import CatalogAsset, CatalogSourceError
from palmimo_portal.testing.fakes import FakeCatalogSource, FakeStateStore, make_catalog_app


def _cache(source: FakeCatalogSource, state: FakeStateStore, *, monotonic_value: list[float]) -> CatalogCache:
    return CatalogCache(
        source,
        state,
        monotonic=lambda: monotonic_value[0],
        wall_clock=lambda: 1000.0 + monotonic_value[0],
    )


def test_get_serves_a_cached_copy_within_the_ttl_without_refetching() -> None:
    source = FakeCatalogSource(asset=CatalogAsset(tag="v1.0.0", apps=(make_catalog_app(),)))
    state = FakeStateStore()
    clock = [0.0]
    cache = _cache(source, state, monotonic_value=clock)

    first = cache.get(ntp_synchronized=True)
    clock[0] = 1800.0  # under CATALOG_TTL_SECONDS (3600)
    second = cache.get(ntp_synchronized=True)

    assert first.tag == "v1.0.0"
    assert second.tag == "v1.0.0"
    assert second.stale is False
    assert source.fetch_calls == 1


def test_get_refetches_once_the_ttl_expires() -> None:
    source = FakeCatalogSource(asset=CatalogAsset(tag="v1.0.0", apps=()))
    state = FakeStateStore()
    clock = [0.0]
    cache = _cache(source, state, monotonic_value=clock)
    cache.get(ntp_synchronized=True)

    clock[0] = 3601.0
    cache.get(ntp_synchronized=True)

    assert source.fetch_calls == 2


def test_get_serves_last_good_with_stale_offline_when_refresh_fails_after_a_success() -> None:
    source = FakeCatalogSource(asset=CatalogAsset(tag="v1.0.0", apps=()))
    state = FakeStateStore()
    clock = [0.0]
    cache = _cache(source, state, monotonic_value=clock)
    cache.get(ntp_synchronized=True)

    source.asset = None
    source.raise_on_fetch = CatalogSourceError("network down")
    clock[0] = 3601.0
    snapshot = cache.get(ntp_synchronized=True)

    assert snapshot.tag == "v1.0.0"
    assert snapshot.stale is True
    assert snapshot.reason == "offline"


def test_get_reports_clock_unsynced_without_attempting_a_fetch() -> None:
    source = FakeCatalogSource()
    state = FakeStateStore()
    cache = _cache(source, state, monotonic_value=[0.0])

    snapshot = cache.get(ntp_synchronized=False)

    assert source.fetch_calls == 0
    assert snapshot.stale is True
    assert snapshot.reason == "clock_unsynced"


def test_invalid_asset_is_rejected_and_the_last_good_copy_is_kept() -> None:
    source = FakeCatalogSource(asset=CatalogAsset(tag="v1.0.0", apps=()))
    state = FakeStateStore()
    clock = [0.0]
    cache = _cache(source, state, monotonic_value=clock)
    cache.get(ntp_synchronized=True)

    source.asset = None
    source.raise_on_fetch = CatalogSourceError("v2.0.0 asset failed schema validation")
    clock[0] = 3601.0
    snapshot = cache.get(ntp_synchronized=True)

    assert snapshot.tag == "v1.0.0"
    assert snapshot.stale is True


def test_get_reports_rate_limited_reason_when_the_source_is_rate_limited() -> None:
    source = FakeCatalogSource(raise_on_fetch=CatalogSourceError("GitHub rate limit hit", code="rate_limited"))
    state = FakeStateStore()
    cache = _cache(source, state, monotonic_value=[0.0])

    snapshot = cache.get(ntp_synchronized=True)

    assert snapshot.reason == "rate_limited"


def test_get_backs_off_after_a_failed_refresh_then_retries_once_the_backoff_elapses() -> None:
    source = FakeCatalogSource(raise_on_fetch=CatalogSourceError("network down"))
    state = FakeStateStore()
    clock = [0.0]
    cache = _cache(source, state, monotonic_value=clock)

    cache.get(ntp_synchronized=True)
    clock[0] = 100.0  # inside the 300s backoff window
    cache.get(ntp_synchronized=True)
    assert source.fetch_calls == 1

    clock[0] = 301.0  # past the backoff window
    cache.get(ntp_synchronized=True)
    assert source.fetch_calls == 2


def test_persisted_last_good_survives_a_restart() -> None:
    state = FakeStateStore()
    first_process_source = FakeCatalogSource(asset=CatalogAsset(tag="v1.0.0", apps=(make_catalog_app(),)))
    first_cache = _cache(first_process_source, state, monotonic_value=[0.0])
    first_cache.get(ntp_synchronized=True)

    # A fresh CatalogCache instance (a new Portal process) sharing the same state store, whose
    # own source is offline -- it must still answer from what the prior instance persisted.
    second_process_source = FakeCatalogSource(raise_on_fetch=CatalogSourceError("offline"))
    second_cache = _cache(second_process_source, state, monotonic_value=[0.0])

    snapshot = second_cache.get(ntp_synchronized=True)

    assert snapshot.tag == "v1.0.0"
    assert len(snapshot.apps) == 1
    assert snapshot.stale is True
