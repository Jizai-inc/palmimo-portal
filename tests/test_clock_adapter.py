"""Tests for :mod:`palmimo_portal.adapters.clock`."""

from __future__ import annotations

import pytest

from palmimo_portal.adapters.clock import NTP_SYNC_CACHE_SECONDS, SystemClockPort


class _StubbedClockPort(SystemClockPort):
    """Stubs the D-Bus round trip so `ntp_synchronized`'s cache can be tested without a real bus."""

    def __init__(self, result: bool) -> None:
        super().__init__()
        self._result = result
        self.read_calls = 0

    async def _read_ntp_synchronized(self) -> bool:
        self.read_calls += 1
        return self._result


def test_ntp_synchronized_reuses_the_cached_result_within_the_cache_window(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("palmimo_portal.adapters.clock.time.monotonic", lambda: now[0])
    port = _StubbedClockPort(True)

    first = port.ntp_synchronized()
    now[0] += NTP_SYNC_CACHE_SECONDS - 1
    second = port.ntp_synchronized()

    assert first is True
    assert second is True
    assert port.read_calls == 1


def test_ntp_synchronized_re_reads_once_the_cache_window_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("palmimo_portal.adapters.clock.time.monotonic", lambda: now[0])
    port = _StubbedClockPort(True)

    port.ntp_synchronized()
    now[0] += NTP_SYNC_CACHE_SECONDS + 1
    port.ntp_synchronized()

    assert port.read_calls == 2
