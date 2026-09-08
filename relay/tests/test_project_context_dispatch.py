"""Optional legacy project context and required facade context must be tenant-bound."""

import base64
import hashlib
from dataclasses import replace

import pytest
from asgiref.sync import sync_to_async
from codito_protocol import TunnelEnvelope
from django.utils import timezone
from test_dispatch import principal_for
from test_screenshot_wire_image import PNG

from codito_relay.core.dispatch import InMemoryTransport, ToolDispatchError, dispatch_tool
from codito_relay.core.models import Device, Operation, Project

CASES = [
    (
        "device_screenshot",
        "screen:read",
        {"action": "capture", "purpose": "Synthetic image test"},
        {
            "width": 1,
            "height": 1,
            "captured_at": timezone.now().isoformat(),
            "sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
            "image_base64": PNG,
        },
    ),
    (
        "device_desktop",
        "shell:execute",
        {"url": "https://example.com/", "purpose": "Synthetic browser test"},
        {"url": "https://example.com/", "status": "submitted"},
    ),
    (
        "device_read",
        "files:read",
        {"operation": "list_directory", "scope_path": "C:/Temp", "purpose": "Synthetic read"},
        {"operation": "list_directory", "scope_path": "C:/Temp", "path": "", "entries": []},
    ),
]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "scope", "arguments", "result"), CASES)
async def test_device_tool_context_binds_real_project_and_scope(
    device, link, name, scope, arguments, result
):
    device.status = Device.Status.ONLINE
    device.connection_epoch = 3
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Context",
        root_fingerprint="c" * 64,
        mode=Project.Mode.FULL_ACCESS,
    )
    arguments = {**arguments, "project_id": project.pk}

    async def handler(raw):
        envelope = TunnelEnvelope.model_validate(raw)
        assert envelope.bindings.project_id == project.pk
        assert envelope.bindings.account_id == f"account_{device.account_id:016x}"
        assert envelope.bindings.device_id == str(device.pk)
        assert envelope.bindings.link_id == str(link.link_id)
        assert envelope.payload["input"]["project_id"] == project.pk
        return {"ok": True, "text": "Synthetic only", "result": result}

    principal = replace(principal_for(device, link), scopes=frozenset({scope}))
    transport = InMemoryTransport(handler)
    with pytest.raises(ToolDispatchError) as missing:
        await dispatch_tool(principal, name, arguments, transport=transport)
    assert missing.value.code == "insufficient_scope"
    assert transport.envelopes == []
    principal = replace(principal, scopes=frozenset({scope, "projects:read"}))
    receipt = await dispatch_tool(principal, name, arguments, transport=transport)
    row = await sync_to_async(Operation.objects.get)(pk=receipt.operation_id)
    assert row.project_id == project.pk
    assert row.status == Operation.Status.SUCCEEDED


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "scope", "arguments", "result"), CASES)
@pytest.mark.parametrize("mismatch", ["unknown", "other_account", "other_device", "unavailable"])
async def test_context_cannot_substitute_unknown_or_unauthorized_project(
    device, link, other_user, name, scope, arguments, result, mismatch
):
    project_id = "project_missing_abcdefghijkl"
    if mismatch != "unknown":
        owner = other_user if mismatch == "other_account" else device.account
        target_device = device
        if mismatch in {"other_device", "other_account"}:
            target_device = await sync_to_async(Device.objects.create)(
                account=owner, name="Other", public_key_jwk={}, key_thumbprint=mismatch
            )
        project = await sync_to_async(Project.objects.create)(
            account=owner,
            device=target_device,
            title="Same title",
            root_fingerprint="d" * 64,
            status=Project.Status.UNAVAILABLE
            if mismatch == "unavailable"
            else Project.Status.AVAILABLE,
        )
        project_id = project.pk
    principal = replace(principal_for(device, link), scopes=frozenset({scope, "projects:read"}))
    transport = InMemoryTransport()
    with pytest.raises(ToolDispatchError) as failure:
        await dispatch_tool(
            principal, name, {**arguments, "project_id": project_id}, transport=transport
        )
    assert failure.value.code == "project_not_found"
    assert transport.envelopes == []
    assert await sync_to_async(Operation.objects.count)() == 0
