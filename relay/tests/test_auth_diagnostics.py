from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlencode

import pytest
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.test import Client, RequestFactory
from oauth2_provider.models import Application
from starlette.types import Message, Receive, Scope, Send

from codito_relay.core import diagnostics

CANARY = "sensitive-canary-password-code-token"


@pytest.fixture
def events(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(diagnostics.logger, "propagate", True)
    caplog.set_level("INFO", logger=diagnostics.logger.name)

    def collected() -> list[dict[str, Any]]:
        records = [r for r in caplog.records if r.name == diagnostics.logger.name]
        assert CANARY not in "\n".join(r.getMessage() for r in records)
        return [json.loads(r.getMessage()) for r in records]

    return collected


async def no_receive() -> Message:
    raise AssertionError("Diagnostics must not consume the request stream")


async def no_send(message: Message) -> None:
    pass


@pytest.mark.asyncio
async def test_asgi_stream_and_concurrent_request_ids(events: Any) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert receive is no_receive
        await asyncio.sleep(0)
        diagnostics.emit("test.child")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": CANARY.encode(), "more_body": True})
        await send({"type": "http.response.body", "body": b"end", "more_body": False})

    middleware = diagnostics.AuthDiagnosticASGI(app)

    async def call() -> list[Message]:
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "path": f"/mcp/d/{CANARY}",
                "method": "POST",
                "headers": [
                    (b"authorization", CANARY.encode()),
                    (b"x-codito-request-id", CANARY.encode()),
                ],
                "query_string": f"code={CANARY}".encode(),
            },
            no_receive,
            send,
        )
        return sent

    first, second = await asyncio.gather(call(), call())
    assert first[1]["body"] == CANARY.encode()
    assert first[1]["more_body"] is True
    assert first[2]["body"] == b"end"
    ids = {
        dict(messages[0]["headers"])[b"x-codito-request-id"].decode()
        for messages in (first, second)
    }
    assert len(ids) == 2
    assert {e["request_id"] for e in events()} == ids
    assert diagnostics.request_id.get() == "unscoped"


@pytest.mark.asyncio
async def test_asgi_exception_is_redacted_and_context_resets(events: Any) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError(CANARY)

    with pytest.raises(RuntimeError, match=CANARY):
        await diagnostics.AuthDiagnosticASGI(app)(
            {"type": "http", "path": "/o/token/", "method": "POST", "headers": []},
            no_receive,
            no_send,
        )
    assert [e["event"] for e in events()] == ["http.start", "http.exception", "http.end"]
    assert diagnostics.request_id.get() == "unscoped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        {"type": "lifespan"},
        {"type": "websocket", "path": "/ws/device"},
        {"type": "http", "path": "/health/ready"},
    ],
)
async def test_unrelated_surfaces_pass_through(scope: Scope, events: Any) -> None:
    async def app(actual: Scope, receive: Receive, send: Send) -> None:
        assert actual is scope
        assert receive is no_receive
        assert send is no_send

    await diagnostics.AuthDiagnosticASGI(app)(scope, no_receive, no_send)
    assert events() == []


def test_oauth_redirect_logs_error_not_secrets(events: Any) -> None:
    request = RequestFactory().post(
        "/o/authorize/",
        {
            "scope": f"openid profile {CANARY}",
            "code": CANARY,
            "resource": f"https://example.test/{CANARY}",
            "client_assertion": CANARY,
            "state": CANARY,
            "client_id": f"https://chatgpt.com/{CANARY}",
            "password": CANARY,
        },
    )
    response = HttpResponseRedirect(
        "https://chatgpt.com/callback?"
        + urlencode(
            {
                "error": "invalid_scope",
                "state": CANARY,
                "error_description": CANARY,
            }
        )
    )
    assert diagnostics.OAuthDiagnosticMiddleware(lambda _: response)(request) is response
    result = events()[0]
    assert result["error"] == "invalid_scope"
    assert result["scopes"] == ["openid", "profile"]
    assert result["unknown_scope_count"] == 1
    assert result["assertion_present"] is True


@pytest.mark.parametrize("error", [None, "invalid_grant", CANARY])
def test_token_body_never_enters_logs(error: str | None, events: Any) -> None:
    request = RequestFactory().post("/o/token/", {"code": CANARY, "code_verifier": CANARY})
    response = JsonResponse(
        {
            "access_token": CANARY,
            "refresh_token": CANARY,
            "error": error,
            "error_description": CANARY,
        }
    )
    assert diagnostics.OAuthDiagnosticMiddleware(lambda _: response)(request) is response
    result = events()[0]
    assert result["access_token_issued"] is True
    assert result["error"] == ("other" if error == CANARY else error)


def test_broken_diagnostic_does_not_change_response(events: Any) -> None:
    request = RequestFactory().post("/o/token/")
    response = HttpResponse(CANARY, status=502, content_type="application/json")
    assert diagnostics.OAuthDiagnosticMiddleware(lambda _: response)(request) is response
    assert events()[0]["event"] == "oauth.diagnostic_unavailable"


@pytest.mark.django_db
def test_real_authorize_scope_failure_has_safe_diagnostics(
    user: Any, link: Any, events: Any
) -> None:
    app = Application.objects.create(
        client_id="https://chatgpt.com/test/client.json",
        name="Diagnostic test",
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://chatgpt.com/connector/oauth/test",
        registration_source=Application.RegistrationSource.CIMD,
    )
    client = Client()
    client.force_login(user)
    response = client.get(
        "/o/authorize/",
        {
            "client_id": app.client_id,
            "redirect_uri": app.redirect_uris,
            "scope": "projects:read openid profile",
            "response_type": "code",
            "resource": link.resource,
            "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
            "state": CANARY,
        },
        secure=True,
    )
    assert response.status_code == 302
    captured = events()
    assert any(e["event"] == "oauth.scopes_rejected" for e in captured)
    assert any(e["event"] == "oauth.outcome" and e["error"] == "invalid_scope" for e in captured)
