from __future__ import annotations

from typing import Any

from codito_relay.core import redis_async


async def test_shared_redis_reuses_pool_and_closes_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    created: list[FakeRedis] = []

    class FakeRedis:
        def __init__(self) -> None:
            self.closed = 0

        async def aclose(self, *, close_connection_pool: bool) -> None:
            assert close_connection_pool is True
            self.closed += 1

    class FakePool:
        @classmethod
        def from_url(cls, _url: str, **_kwargs: Any) -> FakePool:
            return cls()

    def create(*, connection_pool: FakePool) -> FakeRedis:
        assert isinstance(connection_pool, FakePool)
        client = FakeRedis()
        created.append(client)
        return client

    monkeypatch.setattr(redis_async, "BlockingConnectionPool", FakePool)
    monkeypatch.setattr(redis_async, "Redis", create)
    async with redis_async.shared_redis("redis://example/1") as first:
        async with redis_async.shared_redis("redis://example/1") as second:
            assert first is second
    async with redis_async.shared_redis("redis://example/2") as third:
        assert third is not first

    assert len(created) == 2
    assert all(client.closed == 0 for client in created)

    await redis_async.close_shared_redis()

    assert all(client.closed == 1 for client in created)
