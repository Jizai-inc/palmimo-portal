"""Tests for :mod:`palmimo_portal.adapters.dbus_support`'s sync/async bridge.

No real D-Bus involved -- these only exercise the event-loop-thread
plumbing that lets a synchronous Port method drive an ``asyncio`` coroutine.
"""

from __future__ import annotations

import asyncio

import pytest

from palmimo_portal.adapters.dbus_support import SharedEventLoopThread, get_shared_loop_thread


def test_run_returns_the_coroutines_result() -> None:
    thread = SharedEventLoopThread()

    async def compute() -> int:
        return 41 + 1

    assert thread.run(compute(), timeout=5.0) == 42


def test_run_propagates_the_coroutines_exception() -> None:
    thread = SharedEventLoopThread()

    async def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        thread.run(boom(), timeout=5.0)


def test_run_raises_timeout_error_when_the_coroutine_is_too_slow() -> None:
    thread = SharedEventLoopThread()

    async def slow() -> None:
        await asyncio.sleep(10)

    import concurrent.futures

    with pytest.raises(concurrent.futures.TimeoutError):
        thread.run(slow(), timeout=0.05)


def test_run_reuses_the_same_background_loop_across_calls() -> None:
    """Two calls observe the same running loop -- the connection-caching design depends on this."""
    thread = SharedEventLoopThread()
    seen_loops = []

    async def record_loop() -> None:
        seen_loops.append(asyncio.get_running_loop())

    thread.run(record_loop(), timeout=5.0)
    thread.run(record_loop(), timeout=5.0)

    assert seen_loops[0] is seen_loops[1]


def test_get_shared_loop_thread_returns_a_singleton() -> None:
    first = get_shared_loop_thread()
    second = get_shared_loop_thread()

    assert first is second
