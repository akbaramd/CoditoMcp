import base64
import hashlib
from dataclasses import replace

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone
from test_dispatch import principal_for

from codito_relay.core.dispatch import (
    InMemoryTransport,
    ToolDispatchError,
    dispatch_tool,
    success_tool_result,
)
from codito_relay.core.models import Device, Operation

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA9hAAAPYQGoP6dp"
    "AAAADElEQVQImWNgYGAAAAAEAAGjChXjAAAAAElFTkSuQmCC"
)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_screenshot_scope_device_route_image_and_journal(device, link):
    device.status = Device.Status.ONLINE
    device.connection_epoch = 3
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    called = []

    async def handler(envelope):
        assert envelope["bindings"].get("project_id") is None
        assert envelope["payload"]["tool_name"] == "device_screenshot"
        called.append(envelope)
        return {
            "ok": True,
            "text": "Screenshot",
            "result": {
                "width": 1,
                "height": 1,
                "captured_at": timezone.now().isoformat(),
                "sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
                "image_base64": PNG,
            },
        }

    args = {"purpose": "Review screen"}
    transport = InMemoryTransport(handler)
    with pytest.raises(ToolDispatchError) as error:
        await dispatch_tool(
            principal_for(device, link), "device_screenshot", args, transport=transport
        )
    assert error.value.code == "insufficient_scope"
    assert called == []
    principal = replace(principal_for(device, link), scopes=frozenset({"screen:read"}))
    receipt = await dispatch_tool(principal, "device_screenshot", args, transport=transport)
    rendered = success_tool_result(receipt)
    assert rendered["content"][1] == {"type": "image", "data": PNG, "mimeType": "image/png"}
    assert "image_base64" not in rendered["structuredContent"]["result"]
    row = await sync_to_async(Operation.objects.get)(pk=receipt.operation_id)
    assert PNG not in str(row.result)


@pytest.mark.asyncio
async def test_sdk_returns_native_image_content(monkeypatch):
    from codito_relay.core import mcp_server
    from codito_relay.core.dispatch import DispatchReceipt

    async def dispatch(*args):
        return DispatchReceipt(
            "screenshot_test",
            {
                "ok": True,
                "result": {
                    "width": 1,
                    "height": 1,
                    "captured_at": timezone.now().isoformat(),
                    "sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
                    "image_base64": PNG,
                },
            },
        )

    monkeypatch.setattr(mcp_server, "_principal", lambda _: None)
    monkeypatch.setattr(mcp_server, "dispatch_tool", dispatch)
    result = await mcp_server.device_screenshot(None, purpose="Synthetic screenshot")
    assert result.content[1].type == "image"
    assert result.content[1].data == PNG


@pytest.mark.asyncio
async def test_shell_mcp_wrapper_preserves_external_request(monkeypatch):
    from codito_relay.core import mcp_server

    captured = {}

    async def run(context, name, arguments):
        captured.update(arguments)

    monkeypatch.setattr(mcp_server, "_run", run)
    await mcp_server.project_shell(
        None,
        "start",
        "project_abcdefghijkl",
        external_working_directory="C:/",
        requested_external_paths=["C:/"],
        approval_timeout_seconds=240,
    )
    assert captured["external_working_directory"] == "C:/"
    assert captured["approval_timeout_seconds"] == 240
    assert captured["requested_external_paths"] == ["C:/"]
