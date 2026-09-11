from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from django.conf import settings
from django.db import close_old_connections
from prometheus_client import Gauge, Histogram

_executor: ThreadPoolExecutor | None = None
_executor_guard = threading.Lock()
_awaiting_calls = Gauge(
    "codito_database_calls_awaiting",
    "Async relay callers currently awaiting synchronous ORM work.",
)
_active_calls = Gauge(
    "codito_database_calls_active",
    "Synchronous ORM calls currently active in this relay worker.",
)
_queue_seconds = Histogram(
    "codito_database_queue_seconds",
    "Time an ORM unit waits for a bounded relay database lane.",
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5),
)
_call_seconds = Histogram(
    "codito_database_call_seconds",
    "Execution time of a synchronous relay ORM unit.",
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5),
)


def _database_executor() -> ThreadPoolExecutor:
    """Return the process-wide bounded executor used by complete ORM units of work."""

    global _executor
    if _executor is not None:
        return _executor
    with _executor_guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=settings.DATABASE_ASYNC_WORKERS,
                thread_name_prefix="codito-db",
            )
    return _executor


def _run_database_call[ResultT](
    function: Callable[..., ResultT],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    submitted_at: float,
) -> ResultT:
    # Django connections are thread-affine. Every submitted callable is a complete
    # database unit of work and obtains only the current worker's connection.
    _queue_seconds.observe(time.perf_counter() - submitted_at)
    _active_calls.inc()
    started_at = time.perf_counter()
    close_old_connections()
    try:
        return function(*args, **kwargs)
    finally:
        # Retain healthy persistent connections, but discard expired/broken ones
        # before this worker accepts an unrelated request.
        close_old_connections()
        _call_seconds.observe(time.perf_counter() - started_at)
        _active_calls.dec()


async def database_sync[ResultT](
    function: Callable[..., ResultT], /, *args: Any, **kwargs: Any
) -> ResultT:
    """Run one self-contained synchronous Django ORM call without global serialization."""

    loop = asyncio.get_running_loop()
    call = partial(_run_database_call, function, args, kwargs, time.perf_counter())
    _awaiting_calls.inc()
    try:
        return await loop.run_in_executor(_database_executor(), call)
    finally:
        _awaiting_calls.dec()
