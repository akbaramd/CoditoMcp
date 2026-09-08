from __future__ import annotations

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from codito_relay.core.middleware import PublicRateLimitMiddleware, _rate_limit_category
from codito_relay.health import _database_ready


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/accounts/login/", "login"),
        ("/invite/do-not-store-this-selector/", "invitation"),
        ("/reset/do-not-store-this-selector/", "password_reset"),
        ("/o/authorize/", "oauth_authorize"),
        ("/o/token/", "oauth_token"),
        ("/o/revoke_token/", "oauth_token"),
        ("/api/devices/enroll/", "device_enroll"),
        ("/api/devices/12345678-1234-1234-1234-123456789012/revoke/", "device_enroll"),
        ("/api/devices/12345678-1234-1234-1234-123456789012/tickets/", "device_ticket"),
        ("/health/live", None),
    ],
)
def test_public_rate_limit_routes(path: str, expected: str | None) -> None:
    assert _rate_limit_category(path) == expected


@override_settings(
    RATE_LIMIT_ENABLED=True,
    PUBLIC_RATE_LIMITS={"invitation": (1, 60)},
    RATE_LIMIT_KEY_PREFIX="test:rate",
)
def test_rate_limit_uses_redacted_key_and_returns_retry_after(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def eval(self, _script: str, _key_count: int, key: str, window: int) -> list[int]:
            self.calls.append((key, window))
            return [len(self.calls), 42]

    fake = FakeRedis()
    monkeypatch.setattr("codito_relay.core.middleware._new_rate_limit_client", lambda: fake)
    middleware = PublicRateLimitMiddleware(lambda _request: HttpResponse("ok"))
    request = RequestFactory().get(
        "/invite/do-not-store-this-selector/", REMOTE_ADDR="198.51.100.10"
    )
    assert middleware(request).status_code == 200
    limited = middleware(request)
    assert limited.status_code == 429
    assert limited["Retry-After"] == "42"
    assert "do-not-store-this-selector" not in fake.calls[0][0]


@override_settings(
    RATE_LIMIT_ENABLED=True,
    PUBLIC_RATE_LIMITS={"oauth_token": (10, 60)},
    RATE_LIMIT_KEY_PREFIX="test:rate",
)
def test_rate_limit_fails_closed_when_redis_is_unavailable(monkeypatch) -> None:
    class BrokenRedis:
        def eval(self, *_args: object) -> list[int]:
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(
        "codito_relay.core.middleware._new_rate_limit_client", lambda: BrokenRedis()
    )
    middleware = PublicRateLimitMiddleware(lambda _request: HttpResponse("unsafe"))
    response = middleware(RequestFactory().post("/o/token/", REMOTE_ADDR="198.51.100.11"))
    assert response.status_code == 503
    assert response["Retry-After"] == "5"


@pytest.mark.django_db
def test_database_readiness_requires_migrated_core_table(monkeypatch) -> None:
    _database_ready()
    monkeypatch.setattr(
        "django.db.backends.base.introspection.BaseDatabaseIntrospection.table_names",
        lambda *_args, **_kwargs: [],
    )
    with pytest.raises(RuntimeError, match="core migrations"):
        _database_ready()
