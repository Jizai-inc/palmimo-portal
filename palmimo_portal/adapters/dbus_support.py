"""Shared plumbing that lets a synchronous Port method drive dbus-fast's asyncio API.

:class:`~palmimo_portal.ports.NetworkPort`/:class:`~palmimo_portal.ports.SystemPort`
are synchronous (FastAPI's sync threadpool); dbus-fast
(:mod:`dbus_fast.aio`) is asyncio-only. A per-call ``asyncio.run()`` would
tear down and reconnect the D-Bus connection on every request, defeating the
"lazy-connect once, reuse, reconnect only on drop" design the adapters want
(comitup's ``access_points()`` alone can take several seconds). Instead,
:class:`SharedEventLoopThread` runs one dedicated event loop forever in a
background thread; adapters submit coroutines via
:meth:`SharedEventLoopThread.run`, which blocks for the result, so the
``dbus_fast.aio.MessageBus`` they hold between calls stays on that one loop
and can be cached like a socket. One loop is shared per process
(:func:`get_shared_loop_thread` is a lazy singleton), since at most two
real adapters run per process.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, TypeVar


logger = logging.getLogger("palmimo_portal")

_T = TypeVar("_T")


class SharedEventLoopThread:
    """Runs one ``asyncio`` event loop forever in a dedicated daemon thread.

    Started eagerly in ``__init__``, not lazily on first :meth:`run` --
    avoids a race between :meth:`run` being called and ``run_forever``
    actually being scheduled.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve_forever, name="palmimo-portal-dbus", daemon=True)
        self._thread.start()

    def _serve_forever(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, _T], timeout: float) -> _T:
        """Submit *coro* to the background loop and block for its result, up to *timeout* seconds.

        Raises:
            concurrent.futures.TimeoutError: *coro* did not complete within
                *timeout*. The coroutine is cancelled on the background loop
                (best-effort) so it doesn't keep running unobserved.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise


_shared_loop_thread: SharedEventLoopThread | None = None
_shared_loop_thread_lock = threading.Lock()


def get_shared_loop_thread() -> SharedEventLoopThread:
    """Return the process-wide :class:`SharedEventLoopThread`, creating it on first use."""
    global _shared_loop_thread
    with _shared_loop_thread_lock:
        if _shared_loop_thread is None:
            _shared_loop_thread = SharedEventLoopThread()
        return _shared_loop_thread
