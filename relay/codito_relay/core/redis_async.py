from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from django.conf import settings
from redis.asyncio import BlockingConnectionPool, Redis

_clients: dict[tuple[str, bool], Redis[Any]] = {}
_clients_guard = threading.Lock()


def _shared_client(url: str, *, decode_responses: bool) -> Redis[Any]:
    key = (url, decode_responses)
    client = _clients.get(key)
    if client is not None:
        return client
    with _clients_guard:
        client = _clients.get(key)
        if client is None:
            pool: BlockingConnectionPool[Any] = BlockingConnectionPool.from_url(
                url,
                decode_responses=decode_responses,
                max_connections=settings.REDIS_ASYNC_MAX_CONNECTIONS,
                timeout=settings.REDIS_ASYNC_POOL_WAIT_SECONDS,
            )
            client = Redis(connection_pool=pool)
            _clients[key] = client
    return client


@asynccontextmanager
async def shared_redis(
    url: str | None = None, *, decode_responses: bool = True
) -> AsyncIterator[Redis[Any]]:
    """Lease the worker's shared async Redis pool; the ASGI lifespan owns shutdown."""

    yield _shared_client(url or settings.REDIS_URL, decode_responses=decode_responses)


async def close_shared_redis() -> None:
    with _clients_guard:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        # redis-py >= 6 exposes aclose; the retained types-redis 4.x package does not.
        await cast(Any, client).aclose(close_connection_pool=True)
