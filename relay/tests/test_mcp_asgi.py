from __future__ import annotations

import pytest
from mcp_types.version import LATEST_HANDSHAKE_VERSION
from starlette.testclient import TestClient

from codito_relay.asgi import application


@pytest.mark.django_db(transaction=True)
def test_mcp_initialization_list_and_local_tool_call(oauth_token, link) -> None:  # type: ignore[no-untyped-def]
    raw, _ = oauth_token
    headers = {
        "Authorization": f"Bearer {raw}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    endpoint = f"/mcp/d/{link.link_id}"
    with TestClient(application) as client:
        unauthenticated = client.post(endpoint, json={})
        assert unauthenticated.status_code == 401
        assert (
            f"/.well-known/oauth-protected-resource/mcp/d/{link.link_id}"
            in unauthenticated.headers["www-authenticate"]
        )
        initialized = client.post(
            endpoint,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_HANDSHAKE_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "relay-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200, initialized.text
        negotiated = initialized.json()["result"]["protocolVersion"]
        versioned_headers = {**headers, "MCP-Protocol-Version": negotiated}
        tools = client.post(
            endpoint,
            headers=versioned_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert tools.status_code == 200, tools.text
        descriptors = tools.json()["result"]["tools"]
        assert [tool["name"] for tool in descriptors] == [
            "project_read",
            "project_apply_patch",
            "project_shell",
        ]
        assert all(tool.get("_meta", {}).get("securitySchemes") for tool in descriptors)
        called = client.post(
            endpoint,
            headers=versioned_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "project_read", "arguments": {"operation": "list_projects"}},
            },
        )
        assert called.status_code == 200, called.text
        result = called.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["operation_id"] == "local"
