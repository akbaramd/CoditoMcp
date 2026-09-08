"""Selected surfaces, allowlisted values, and server-generated correlation IDs.

Never pass request bodies, URLs, exception messages, or credentials to ``emit``.
These diagnostics deliberately do not enable Django/oauthlib debug logging.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from codito_relay import __version__

request_id: ContextVar[str] = ContextVar("codito_diagnostic_request_id", default="unscoped")
logger = logging.getLogger("codito.auth_diagnostics")
OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "invalid_scope",
        "invalid_target",
        "invalid_token",
        "insufficient_scope",
        "access_denied",
        "unauthorized_client",
        "unsupported_grant_type",
        "unsupported_response_type",
        "server_error",
        "temporarily_unavailable",
        "rate_limited",
        "login_required",
        "consent_required",
        "interaction_required",
    }
)


def emit(event: str, **fields: Any) -> None:
    logger.info(
        json.dumps(
            {
                "time": datetime.now(UTC).isoformat(),
                "event": event,
                "request_id": request_id.get(),
                "version": __version__,
                **fields,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


def safe_error(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in OAUTH_ERRORS else "other"


def scope_summary(scopes: list[str]) -> dict[str, Any]:
    known = set(cast(dict[str, str], settings.OAUTH2_PROVIDER["SCOPES"]))
    return {
        "scopes": sorted(set(scopes) & known),
        "unknown_scope_count": len(set(scopes) - known),
    }


def diagnostic_route(path: str) -> str | None:
    routes = {
        "/accounts/login/": "login",
        "/accounts/logout/": "logout",
        "/o/authorize/": "oauth_authorize",
        "/o/token/": "oauth_token",
        "/o/revoke_token/": "oauth_revoke",
        "/o/introspect/": "oauth_introspect",
        "/oidc/userinfo": "oidc_userinfo",
        "/.well-known/oauth-authorization-server": "oauth_discovery",
        "/.well-known/openid-configuration": "oidc_discovery",
        "/.well-known/jwks.json": "oidc_jwks",
    }
    if path.startswith("/.well-known/oauth-protected-resource/"):
        return "resource_discovery"
    if path.startswith("/mcp/d/"):
        return "mcp"
    return routes.get(path)


class AuthDiagnosticASGI:
    """Observe HTTP without consuming bodies or changing streaming behavior."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        route = diagnostic_route(scope.get("path", ""))
        if scope["type"] != "http" or route is None:
            await self.app(scope, receive, send)
            return
        token = request_id.set(uuid4().hex)
        started = monotonic()
        status: int | None = None
        method = scope.get("method", "")
        method = method if method in {"GET", "POST", "DELETE", "OPTIONS", "HEAD"} else "other"
        emit(
            "http.start",
            route=route,
            method=method,
            authorization_present=any(k.lower() == b"authorization" for k, _ in scope["headers"]),
        )

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                # Never reflect the caller's ID (or copy its headers) into logs.
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"x-codito-request-id"
                ]
                message = {
                    **message,
                    "headers": [
                        *headers,
                        (
                            b"x-codito-request-id",
                            request_id.get().encode("ascii"),
                        ),
                    ],
                }
                emit(
                    "http.response",
                    route=route,
                    status=status,
                    elapsed_ms=round((monotonic() - started) * 1000),
                )
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except Exception:
            # Exceptions may contain OAuth credentials. Only log the fixed phase.
            emit("http.exception", route=route, status=status)
            raise
        finally:
            emit(
                "http.end",
                route=route,
                status=status,
                elapsed_ms=round((monotonic() - started) * 1000),
            )
            request_id.reset(token)


class OAuthDiagnosticMiddleware:
    """Read only safe outcome fields after Django has handled the request."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if diagnostic_route(request.path) not in {"oauth_authorize", "oauth_token", "login"}:
            return response
        try:
            self._record(request, response)
        except Exception:
            # Malformed/oversized input must not turn diagnostic collection into
            # a new auth failure, and exception text must never expose that input.
            emit("oauth.diagnostic_unavailable")
        return response

    @staticmethod
    def _record(request: HttpRequest, response: HttpResponse) -> None:
        fields: dict[str, Any] = {"status": response.status_code}
        if request.path != "/accounts/login/":
            params = request.POST if request.method == "POST" else request.GET
            scopes = params.get("scope", "").split()
            fields.update(scope_summary(scopes))
            fields.update(
                {
                    "scope_present": "scope" in params,
                    "resource_count": len(params.getlist("resource")),
                    "pkce_challenge_present": bool(params.get("code_challenge")),
                    "pkce_verifier_present": bool(params.get("code_verifier")),
                    "assertion_present": bool(params.get("client_assertion")),
                    "code_present": bool(params.get("code")),
                    "refresh_token_present": bool(params.get("refresh_token")),
                    "client_kind": (
                        "desktop"
                        if params.get("client_id") == settings.DESKTOP_OAUTH_CLIENT_ID
                        else "cimd"
                        if params.get("client_id", "").startswith("https://")
                        else "other"
                    ),
                }
            )
        if location := response.get("Location"):
            target = urlsplit(location)
            query = parse_qs(target.query, max_num_fields=32)
            fields["redirect_kind"] = "login" if target.path == settings.LOGIN_URL else "other"
            fields["authorization_code_issued"] = bool(query.get("code"))
            fields["error"] = safe_error(query.get("error", [None])[0])
        elif (
            response.get("Content-Type", "").startswith("application/json")
            and not response.streaming
        ):
            # Token responses are small; inspect booleans only, never log values.
            if len(response.content) <= 65_536:
                body = json.loads(response.content)
                if isinstance(body, dict):
                    fields["error"] = safe_error(body.get("error"))
                    fields["access_token_issued"] = bool(body.get("access_token"))
        context = getattr(response, "context_data", None)
        if isinstance(context, dict):
            error = context.get("error")
            if error:
                fields["error"] = safe_error(getattr(error, "error", None))
            form = context.get("form")
            # Boolean only: validation errors can contain submitted passwords/URLs.
            fields["form_invalid"] = bool(form is not None and form.is_bound and form.errors)
        emit("oauth.outcome", route=diagnostic_route(request.path), **fields)
