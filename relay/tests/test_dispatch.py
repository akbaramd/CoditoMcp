from __future__ import annotations

from dataclasses import replace

import pytest
from asgiref.sync import sync_to_async
from codito_protocol import TunnelEnvelope
from django.utils import timezone

from codito_relay.core.authz import MCPPrincipal
from codito_relay.core.dispatch import (
    DispatchReceipt,
    InMemoryTransport,
    OfflineTransport,
    ToolDispatchError,
    dispatch_tool,
    error_tool_result,
    success_tool_result,
)
from codito_relay.core.models import Device, DeviceLink, Operation, Project


def principal_for(device: Device, link) -> MCPPrincipal:  # type: ignore[no-untyped-def]
    return MCPPrincipal(
        account_id=device.account_id,
        oauth_grant_id="grant_0123456789abcdef",
        access_token_id=1,
        device_id=device.pk,
        device_link_id=link.pk,
        link_id=link.link_id,
        resource=link.resource,
        scopes=frozenset(
            {"projects:read", "projects:write", "files:read", "files:write", "shell:execute"}
        ),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_device_read_routes_without_project_but_requires_read_scope(device, link):
    device.status = Device.Status.ONLINE
    device.connection_epoch = 3
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()

    async def handler(raw):
        envelope = TunnelEnvelope.model_validate(raw)
        assert envelope.bindings.project_id is None
        assert envelope.bindings.device_id == str(device.pk)
        assert envelope.bindings.account_id == f"account_{device.account_id:016x}"
        assert envelope.payload["tool_name"] == "device_read"
        return {
            "ok": True,
            "text": "Read",
            "result": {
                "operation": "list_directory",
                "scope_path": "C:/",
                "path": "",
                "entries": [],
            },
        }

    args = {"operation": "list_directory", "scope_path": "C:/", "purpose": "Inspect"}
    receipt = await dispatch_tool(
        principal_for(device, link), "device_read", args, transport=InMemoryTransport(handler)
    )
    assert receipt.result["ok"]
    limited = replace(principal_for(device, link), scopes=frozenset({"projects:read"}))
    with pytest.raises(ToolDispatchError) as error:
        await dispatch_tool(limited, "device_read", args, transport=InMemoryTransport(handler))
    assert error.value.code == "insufficient_scope"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_list_projects_is_local_and_contains_no_path(device, link) -> None:  # type: ignore[no-untyped-def]
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Same title",
        root_fingerprint="f" * 64,
    )
    receipt = await dispatch_tool(
        principal_for(device, link), "project_read", {"operation": "list_projects", "limit": 50}
    )
    assert receipt.operation_id == "local"
    assert receipt.result["projects"][0]["project_id"] == project.pk
    assert "path" not in receipt.result["projects"][0]

    managed = await dispatch_tool(
        principal_for(device, link), "project_manage", {"operation": "get_projects", "limit": 50}
    )
    assert managed.operation_id == "local"
    assert managed.result["operation"] == "get_projects"
    assert managed.result["projects"][0]["project_id"] == project.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_offline_mutation_fails_before_transport(device, link) -> None:  # type: ignore[no-untyped-def]
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Project",
        root_fingerprint="e" * 64,
    )
    with pytest.raises(ToolDispatchError) as caught:
        await dispatch_tool(
            principal_for(device, link),
            "project_apply_patch",
            {
                "project_id": project.pk,
                "patch": "*** Begin Patch\n*** Add File: x.txt\n+hello world\n*** End Patch\n",
                "base_hashes": {"x.txt": None},
                "idempotency_key": "idem_0123456789abcdef",
                "dry_run": False,
            },
            transport=InMemoryTransport(),
        )
    assert caught.value.code == "device_offline"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_online_dispatch_uses_valid_shared_envelope(device, link) -> None:  # type: ignore[no-untyped-def]
    device.status = Device.Status.ONLINE
    device.connection_epoch = 7
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Project",
        root_fingerprint="d" * 64,
    )

    async def handler(raw):  # type: ignore[no-untyped-def]
        envelope = TunnelEnvelope.model_validate(raw)
        assert envelope.bindings.project_id == project.pk
        assert envelope.connection_epoch == 7
        assert envelope.payload["tool_name"] == "project_read"
        assert envelope.payload["input"]["path"] == "README.md"
        return {
            "ok": True,
            "text": "Read README.md.",
            "result": {
                "project_id": project.pk,
                "path": "README.md",
                "operation": "read_file",
                "numbered_text": "1: hello",
                "encoding": "utf-8",
                "newline": "none",
                "size": 5,
                "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
                "first_line": 1,
                "last_line": 1,
                "truncated": False,
            },
        }

    receipt = await dispatch_tool(
        principal_for(device, link),
        "project_read",
        {"operation": "read_file", "project_id": project.pk, "path": "README.md"},
        transport=InMemoryTransport(handler),
    )
    assert receipt.result["result"]["numbered_text"] == "1: hello"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_scope_set_is_enforced(device, link) -> None:  # type: ignore[no-untyped-def]
    limited = replace(principal_for(device, link), scopes=frozenset({"files:read"}))
    with pytest.raises(ToolDispatchError) as caught:
        await dispatch_tool(limited, "project_read", {"operation": "list_projects"})
    assert caught.value.code == "insufficient_scope"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_project_registration_request_is_device_scoped_and_idempotent(device, link) -> None:  # type: ignore[no-untyped-def]
    device.status = Device.Status.ONLINE
    device.connection_epoch = 5
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    arguments = {
        "operation": "request_add_project",
        "title": "New project",
        "idempotency_key": "register_0123456789abcdef",
    }

    async def handler(envelope):  # type: ignore[no-untyped-def]
        assert envelope["bindings"].get("project_id") is None
        return {
            "ok": True,
            "text": "Select a local folder.",
            "result": {
                "operation": "request_add_project",
                "request_id": "project_request_0123456789abcdef",
                "title": "New project",
                "status": "pending_local_selection",
            },
        }

    principal = principal_for(device, link)
    first = await dispatch_tool(
        principal, "project_manage", arguments, transport=InMemoryTransport(handler)
    )
    second = await dispatch_tool(
        principal, "project_manage", arguments, transport=InMemoryTransport(handler)
    )
    assert second.operation_id == first.operation_id
    assert second.result == first.result
    assert await sync_to_async(Operation.objects.filter(kind="project_manage").count)() == 1


def test_device_failure_becomes_mcp_tool_error() -> None:
    receipt = DispatchReceipt(
        operation_id="operation_0123456789abcdef",
        result={
            "ok": False,
            "text": "The user denied this operation.",
            "error": {
                "code": "approval_denied",
                "message": "The user denied this operation.",
                "retryable": False,
                "details": {},
            },
        },
    )
    result = success_tool_result(receipt)
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "approval_denied"


@pytest.mark.parametrize(
    ("internal", "public"),
    [
        ("device_queue_full", "queue_full"),
        ("operation_timeout", "deadline_exceeded"),
        ("insufficient_scope", "scope_missing"),
        ("protocol_error", "internal_error"),
        ("relay_unavailable", "internal_error"),
    ],
)
def test_relay_failures_use_shared_typed_error_codes(internal: str, public: str) -> None:
    result = error_tool_result(ToolDispatchError(internal, "Safe failure"))
    assert result["isError"] is True
    assert result["structuredContent"]["ok"] is False
    assert result["structuredContent"]["error"]["code"] == public


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_idempotency_isolated_by_oauth_grant_and_device_link(device, link) -> None:  # type: ignore[no-untyped-def]
    device.status = Device.Status.ONLINE
    device.connection_epoch = 9
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Project",
        root_fingerprint="8" * 64,
    )
    second_link = await sync_to_async(DeviceLink.objects.create)(
        account=device.account,
        device=device,
        label="Second",
    )
    arguments = {
        "project_id": project.pk,
        "patch": "*** Begin Patch\n*** Add File: x.txt\n+hello world\n*** End Patch\n",
        "base_hashes": {"x.txt": None},
        "idempotency_key": "idem_0123456789abcdef",
        "dry_run": True,
    }

    async def handler(_envelope):  # type: ignore[no-untyped-def]
        return {
            "ok": True,
            "text": "Preflight completed.",
            "result": {
                "project_id": project.pk,
                "idempotency_key": arguments["idempotency_key"],
                "dry_run": True,
                "applied": False,
                "files": [],
                "conflicts": [],
            },
        }

    first = principal_for(device, link)
    second_grant = replace(first, oauth_grant_id="grant_fedcba9876543210")
    second_link_principal = replace(
        second_grant,
        oauth_grant_id="grant_0011223344556677",
        device_link_id=second_link.pk,
        link_id=second_link.link_id,
        resource=second_link.resource,
    )
    for principal in (first, second_grant, second_link_principal):
        await dispatch_tool(
            principal,
            "project_apply_patch",
            arguments,
            transport=InMemoryTransport(handler),
        )

    operations = await sync_to_async(
        Operation.objects.filter(idempotency_key=arguments["idempotency_key"]).count
    )()
    assert operations == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_failed_idempotent_operation_returns_terminal_error(device, link) -> None:  # type: ignore[no-untyped-def]
    device.status = Device.Status.ONLINE
    device.connection_epoch = 4
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Project",
        root_fingerprint="9" * 64,
    )
    arguments = {
        "project_id": project.pk,
        "patch": "*** Begin Patch\n*** Add File: x.txt\n+hello world\n*** End Patch\n",
        "base_hashes": {"x.txt": None},
        "idempotency_key": "idem_terminal_0123456789",
        "dry_run": False,
    }
    for _attempt in range(2):
        with pytest.raises(ToolDispatchError) as caught:
            await dispatch_tool(
                principal_for(device, link),
                "project_apply_patch",
                arguments,
                transport=OfflineTransport(),
            )
        assert caught.value.code == "device_offline"
    count = await sync_to_async(
        Operation.objects.filter(idempotency_key=arguments["idempotency_key"]).count
    )()
    assert count == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_transport_exception_is_typed_and_persisted(device, link) -> None:  # type: ignore[no-untyped-def]
    device.status = Device.Status.ONLINE
    device.connection_epoch = 5
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Project",
        root_fingerprint="0" * 64,
    )

    class BrokenTransport:
        async def publish_and_wait(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise ConnectionError("redis unavailable")

    with pytest.raises(ToolDispatchError) as caught:
        await dispatch_tool(
            principal_for(device, link),
            "project_read",
            {"operation": "read_file", "project_id": project.pk, "path": "README.md"},
            transport=BrokenTransport(),
        )
    assert caught.value.code == "relay_unavailable"
    operation = await sync_to_async(Operation.objects.get)(project=project)
    assert operation.status == Operation.Status.FAILED
    assert operation.error_code == "relay_unavailable"
