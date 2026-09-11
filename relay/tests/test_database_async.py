from __future__ import annotations

import asyncio
import threading

import pytest

from codito_relay.core.database_async import database_sync


@pytest.mark.django_db(transaction=True)
async def test_database_calls_have_bounded_parallel_execution_lanes() -> None:
    barrier = threading.Barrier(2, timeout=2)

    def overlap() -> int:
        barrier.wait()
        return threading.get_ident()

    thread_ids = await asyncio.wait_for(
        asyncio.gather(database_sync(overlap), database_sync(overlap)),
        timeout=3,
    )

    assert len(set(thread_ids)) == 2
