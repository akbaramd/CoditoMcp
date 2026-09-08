from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from time import time as unix_time
from typing import Any

from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse, JsonResponse

_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


def _rate_limit_category(path: str) -> str | None:
    exact = {
        "/accounts/login/": "login",
        "/admin/login/": "login",
        "/o/authorize/": "oauth_authorize",
        "/o/token/": "oauth_token",
        "/o/revoke/": "oauth_token",
        "/o/revoke_token/": "oauth_token",
        "/o/introspect/": "oauth_token",
        "/api/devices/enroll/": "device_enroll",
    }
    if category := exact.get(path):
        return category
    if re.fullmatch(r"/invite/[^/]+/", path):
        return "invitation"
    if re.fullmatch(r"/reset/[^/]+/", path):
        return "password_reset"
    if re.fullmatch(r"/api/devices/[0-9a-fA-F-]+/tickets/", path):
        return "device_ticket"
    if re.fullmatch(r"/api/devices/[0-9a-fA-F-]+/revoke/", path):
        return "device_enroll"
    return None


def _new_rate_limit_client() -> Any:
    from redis import from_url

    return from_url(
        settings.RATE_LIMIT_REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        socket_timeout=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
    )


class PublicRateLimitMiddleware:
    """Redis-backed fixed-window limits for authentication and enrollment surfaces.

    These endpoints fail closed when Redis is unavailable. Keys contain only a
    category plus a digest of the caller identity; invitation/reset selectors
    and bearer credentials never enter Redis or logs.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self._client: Any | None = None

    def _identity_digest(self, request: HttpRequest, category: str) -> str:
        remote_address = request.META.get("REMOTE_ADDR", "unknown")
        authorization = request.headers.get("Authorization", "")
        credential_hint = authorization if category in {"device_enroll", "device_ticket"} else ""
        value = f"{category}\x00{remote_address}\x00{credential_hint}"
        return hashlib.sha256(value.encode()).hexdigest()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not settings.RATE_LIMIT_ENABLED:
            return self.get_response(request)
        category = _rate_limit_category(request.path)
        if category is None:
            return self.get_response(request)
        raw_policy = settings.PUBLIC_RATE_LIMITS.get(category)
        if raw_policy is None:
            return self.get_response(request)
        limit, window_seconds = raw_policy
        key = (
            f"{settings.RATE_LIMIT_KEY_PREFIX}:{category}:"
            f"{self._identity_digest(request, category)}"
        )
        try:
            if self._client is None:
                self._client = _new_rate_limit_client()
            result = self._client.eval(_RATE_LIMIT_SCRIPT, 1, key, window_seconds)
            count, ttl = int(result[0]), max(1, int(result[1]))
        except Exception:
            return JsonResponse(
                {"error": "temporarily_unavailable"},
                status=503,
                headers={"Cache-Control": "no-store", "Retry-After": "5"},
            )
        if count > limit:
            return JsonResponse(
                {"error": "rate_limited"},
                status=429,
                headers={"Cache-Control": "no-store", "Retry-After": str(ttl)},
            )
        return self.get_response(request)


class SessionIdleTimeoutMiddleware:
    """Enforce absolute and idle limits for authenticated browser sessions."""

    last_activity_key = "codito_last_activity"
    started_at_key = "codito_session_started_at"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            return self.get_response(request)

        now = int(unix_time())
        started_at = request.session.get(self.started_at_key)
        last_activity = request.session.get(self.last_activity_key)
        try:
            absolute_expired = started_at is not None and (
                now - int(started_at) >= settings.SESSION_ABSOLUTE_TIMEOUT_SECONDS
            )
            idle_expired = last_activity is not None and (
                now - int(last_activity) >= settings.SESSION_IDLE_TIMEOUT_SECONDS
            )
        except (TypeError, ValueError):
            absolute_expired = idle_expired = True

        if absolute_expired or idle_expired:
            logout(request)
            return self.get_response(request)

        if started_at is None:
            request.session[self.started_at_key] = now
        request.session[self.last_activity_key] = now
        return self.get_response(request)
