"""Device-only browser routing and public MCP contracts, without a browser."""

import uuid
from dataclasses import replace

import pytest
from asgiref.sync import sync_to_async
from codito_protocol import TOOL_CONTRACTS, TunnelEnvelope
from django.utils import timezone
from test_dispatch import principal_for

from codito_relay.core.dispatch import (
    InMemoryTransport,
    ToolDispatchError,
    _validate_device_result,
    dispatch_tool,
)
from codito_relay.core.models import Device, Operation

ARGUMENTS = {
    "action": "open_browser",
    "browser": "firefox",
    "url": "https://example.com/",
    "purpose": "Review application in Firefox",
}


async def online(device):
    device.status = Device.Status.ONLINE
    device.connection_epoch = 7
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()


@pytest.mark.django_db(transaction=True)
async def test_desktop_requires_shell_scope_and_routes_exact_device_without_project(device, link):
    await online(device)
    transport = InMemoryTransport()
    read_only = replace(
        principal_for(device, link), scopes=frozenset({"screen:read", "files:read"})
    )
    with pytest.raises(ToolDispatchError) as error:
        await dispatch_tool(read_only, "device_desktop", ARGUMENTS, transport=transport)
    assert error.value.code == "insufficient_scope"
    assert error.value.details == {"required_scopes": ["shell:execute"]}
    assert transport.envelopes == []
    assert await sync_to_async(Operation.objects.count)() == 0

    async def handler(raw):
        envelope = TunnelEnvelope.model_validate(raw)
        assert envelope.bindings.project_id is None
        assert envelope.bindings.account_id == f"account_{device.account_id:016x}"
        assert envelope.bindings.device_id == str(device.pk)
        assert envelope.bindings.link_id == str(link.link_id)
        assert envelope.bindings.grant_id == "grant_0123456789abcdef"
        assert envelope.connection_epoch == 7
        assert envelope.payload == {"tool_name": "device_desktop", "input": ARGUMENTS}
        return {
            "ok": True,
            "text": "Submitted",
            "result": {"url": ARGUMENTS["url"], "browser": "firefox"},
        }

    execute_only = replace(principal_for(device, link), scopes=frozenset({"shell:execute"}))
    receipt = await dispatch_tool(
        execute_only, "device_desktop", ARGUMENTS, transport=InMemoryTransport(handler)
    )
    assert receipt.result["result"] == {
        "action": "open_browser",
        "browser": "firefox",
        "url": ARGUMENTS["url"],
        "status": "submitted",
    }
    row = await sync_to_async(Operation.objects.get)(pk=receipt.operation_id)
    assert row.device_id == device.pk and row.project_id is None
    assert row.kind == "device_desktop" and row.status == Operation.Status.SUCCEEDED
    assert row.account_id == device.account_id and row.device_link_id == link.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("problem", ["offline", "unknown", "other_account", "revoked", "stale"])
async def test_desktop_unavailable_route_never_creates_operation(device, link, problem):
    await online(device)
    principal = principal_for(device, link)
    expected = "device_not_found"
    if problem == "offline":
        device.status = Device.Status.OFFLINE
        expected = "device_offline"
    elif problem == "unknown":
        principal = replace(principal, device_id=uuid.uuid4())
    elif problem == "other_account":
        principal = replace(principal, account_id=principal.account_id + 1000)
    elif problem == "revoked":
        device.revoked_at = timezone.now()
    else:
        device.last_seen_at = timezone.now() - timezone.timedelta(minutes=10)
        expected = "device_offline"
    await sync_to_async(device.save)()
    transport = InMemoryTransport()
    with pytest.raises(ToolDispatchError) as error:
        await dispatch_tool(principal, "device_desktop", ARGUMENTS, transport=transport)
    assert error.value.code == expected
    assert transport.envelopes == []
    assert await sync_to_async(Operation.objects.count)() == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True},
        {"ok": "true", "result": {"url": "https://example.com/"}},
        {"ok": True, "result": {"url": "file:///C:/Windows/cmd.exe"}},
        {"ok": True, "result": {"url": "https://example.com/", "status": "loaded"}},
        {"ok": True, "result": {"url": "https://example.com/", "browser": "cmd"}},
    ],
)
def test_browser_device_result_is_validated(payload):
    with pytest.raises(ToolDispatchError, match="Device") as error:
        _validate_device_result("device_desktop", payload)
    assert error.value.code == "protocol_error"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("extra", [{"approved": True}, {"browser": "cmd"}, {"url": "file:///tmp"}])
async def test_browser_bad_remote_input_never_dispatches(device, link, extra):
    await online(device)
    transport = InMemoryTransport()
    with pytest.raises(ToolDispatchError) as error:
        await dispatch_tool(
            principal_for(device, link),
            "device_desktop",
            {**ARGUMENTS, **extra},
            transport=transport,
        )
    assert error.value.code == "invalid_request"
    assert transport.envelopes == []
    assert await sync_to_async(Operation.objects.count)() == 0


async def test_browser_mcp_wrapper_forwards_exact_inputs_and_scope(monkeypatch):
    from codito_relay.core import mcp_server

    captured = []

    async def run(context, name, arguments):
        captured.append((name, arguments))
        return "synthetic result"

    monkeypatch.setattr(mcp_server, "_run", run)
    result = await mcp_server.device_desktop(None, **ARGUMENTS)
    assert result == "synthetic result"
    assert captured == [("device_desktop", ARGUMENTS)]
    assert TOOL_CONTRACTS["device_desktop"]["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["shell:execute"]}
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", ["timeout", "connection"])
async def test_browser_transport_uncertainty_is_never_retryable(device, link, failure):
    await online(device)

    class UncertainTransport:
        async def publish_and_wait(self, **kwargs):
            if failure == "timeout":
                raise ToolDispatchError("operation_timeout", "No terminal result", retryable=True)
            raise ConnectionError("Synthetic connection interruption after publish")

    with pytest.raises(ToolDispatchError) as error:
        await dispatch_tool(
            principal_for(device, link), "device_desktop", ARGUMENTS, transport=UncertainTransport()
        )
    assert error.value.code == "outcome_unknown"
    assert not error.value.retryable
    row = await sync_to_async(Operation.objects.get)(kind="device_desktop")
    assert row.status == Operation.Status.OUTCOME_UNKNOWN
