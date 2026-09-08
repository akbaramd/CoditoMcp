from dataclasses import replace

import pytest
from mcp_types.version import LATEST_HANDSHAKE_VERSION
from test_dispatch import principal_for

from codito_relay.core import mcp_server
from codito_relay.core.models import Operation


def assert_screen_scope_challenge_survives_http_sdk_serialization(
    client, oauth_token, link, caplog
):
    raw, token = oauth_token
    assert "screen:read" not in token.scope.split()
    headers = {
        "Authorization": f"Bearer {raw}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": LATEST_HANDSHAKE_VERSION,
    }
    response = client.post(
        f"/mcp/d/{link.link_id}",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "device_screenshot",
                "arguments": {"purpose": "Scope regression, never capture pixels"},
            },
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    challenge = result["_meta"]["mcp/www_authenticate"][0]
    assert f'/.well-known/oauth-protected-resource/mcp/d/{link.link_id}"' in challenge
    assert 'error="insufficient_scope"' in challenge
    assert 'error_description="Reconnect this Codito connection' in challenge
    assert "screen:read" in challenge
    for scope in token.scope.split():
        assert scope in challenge  # Preserve approved permissions when requesting one more.
    assert "device:manage" not in challenge
    assert not Operation.objects.filter(kind="device_screenshot").exists()
    token.refresh_from_db()
    assert "screen:read" not in token.scope.split()  # Never silently upgrade the token.
    assert raw not in response.text
    assert raw not in caplog.text


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_invalid_arguments_do_not_trigger_reauthorization(device, link, monkeypatch):
    monkeypatch.setattr(mcp_server, "_principal", lambda _: principal_for(device, link))
    result = await mcp_server.device_screenshot(None, purpose="")
    assert result.is_error
    assert result.meta is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_screen_authorized_grant_does_not_loop_into_reauthorization(
    device, link, monkeypatch
):
    principal = replace(principal_for(device, link), scopes=frozenset({"screen:read"}))
    monkeypatch.setattr(mcp_server, "_principal", lambda _: principal)
    # The fixture is offline: normal device error, never an OAuth challenge.
    result = await mcp_server.device_screenshot(None, purpose="Scope check")
    assert result.is_error
    assert result.meta is None
