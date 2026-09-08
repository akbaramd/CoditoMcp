"""Facade proof through the existing single-use SDK lifespan; no live device actions."""

import base64
import hashlib

from codito_protocol.facade import facade_wire_request
from codito_protocol.facade_contracts import FacadeToolResult
from django.utils import timezone
from mcp_types.version import LATEST_HANDSHAKE_VERSION
from test_screenshot_wire_image import PNG

from codito_relay.core import mcp_server
from codito_relay.core.dispatch import DispatchReceipt

PROJECT = "project_abcdefghijkl"
KEY = "idempotency_abcdefghijkl"
CONTEXT = {"project_id": PROJECT, "purpose": "Synthetic facade contract test"}
CASES = {
    "projects_list": {},
    "project_add": {"title": "Demo", "idempotency_key": KEY},
    "project_rename": {"project_id": PROJECT, "title": "New", "idempotency_key": KEY},
    "project_remove": {"project_id": PROJECT, "idempotency_key": KEY},
    "directory_list": {**CONTEXT, "scope_path": "C:/Temp"},
    "file_read": {**CONTEXT, "path": "a.txt", "start_line": 10, "max_lines": 4},
    "text_search": {**CONTEXT, "query": "needle", "scope_path": "C:/Temp"},
    "file_patch": {
        **CONTEXT,
        "patch": "*** Begin Patch\n*** Add File: a.txt\n+hello\n*** End Patch\n",
        "base_hashes": {"a.txt": None},
        "idempotency_key": KEY,
    },
    "file_delete": {**CONTEXT, "path": "a.txt", "base_hash": "a" * 64, "idempotency_key": KEY},
    "execute_shell": {
        **CONTEXT,
        "command": "dotnet --info",
        "executor": "cmd",
        "cwd": "C:/Temp",
        "timeout_seconds": 17,
        "idempotency_key": KEY,
    },
    "shell_status": {"project_id": PROJECT, "job_id": "job_abcdefghijkl", "sequence_cursor": 7},
    "shell_cancel": {"project_id": PROJECT, "job_id": "job_abcdefghijkl"},
    "screen_list": {},
    "screenshot_capture": {**CONTEXT, "display": "screen_" + "a" * 64},
    "browser_open": {**CONTEXT, "browser": "firefox", "url": "https://example.com/"},
}


def assert_focused_facade_http(client, oauth_token, link, monkeypatch):
    raw, _ = oauth_token
    headers = {
        "Authorization": f"Bearer {raw}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": LATEST_HANDSHAKE_VERSION,
    }

    def call(name, arguments):
        response = client.post(
            f"/mcp/d/{link.link_id}",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        FacadeToolResult.model_validate(result["structuredContent"])
        return result

    # Real relay-local list proves the legacy result is normalized for the facade.
    listed = call("projects_list", {})
    assert listed["isError"] is False
    assert listed["structuredContent"]["operation_id"] == "local"
    assert listed["structuredContent"]["result"]["operation"] == "list_projects"
    captured = []

    async def dispatch(principal, name, arguments):
        assert principal.link_id == link.link_id
        captured.append((name, arguments))
        payload = {"synthetic": True}
        if name == "device_screenshot" and arguments["action"] == "capture":
            payload = {
                "display": arguments["display"],
                "width": 1,
                "height": 1,
                "captured_at": timezone.now().isoformat(),
                "sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
                "image_base64": PNG,
            }
        return DispatchReceipt(
            "synthetic_facade_operation",
            {"ok": True, "text": "Synthetic result, no device action", "result": payload},
        )

    with monkeypatch.context() as patch:
        patch.setattr(mcp_server, "dispatch_tool", dispatch)
        for name, arguments in CASES.items():
            result = call(name, arguments)
            assert result["isError"] is False, result
            assert captured[-1] == facade_wire_request(name, arguments)
            if name == "screenshot_capture":
                assert result["content"][1]["type"] == "image"
                assert result["content"][1]["data"] == PNG
                assert "image_base64" not in result["structuredContent"]["result"]
        calls_before = len(captured)
        for name, arguments in CASES.items():
            for field in ("approved", "action", "unknown_argument"):
                failure = call(name, {**arguments, field: "must-not-dispatch"})
                assert failure["isError"] is True
                assert failure["structuredContent"]["error"]["code"] == "invalid_request"
        assert len(captured) == calls_before
