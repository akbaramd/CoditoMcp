import base64
import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from codito_protocol import MessageKind, TunnelBindings, TunnelEnvelope, compute_action_digest
from django.test import override_settings
from django.utils import timezone
from test_dispatch import principal_for
from test_screenshot_wire_image import make_png

from codito_relay.core.dispatch import (
    InMemoryTransport,
    ToolDispatchError,
    _is_replay_unsafe,
    _required_scopes,
    _validate_device_result_for_request,
    dispatch_tool,
    success_tool_result,
)
from codito_relay.core.models import Device, Operation, Project
from codito_relay.core.websocket import (
    DeviceConnection,
    _record_device_message,
    _recover_unsent_operations,
)

SESSION_ID = "frontend_session_abcdefghijkl"
SNAPSHOT_ID = "frontend_snapshot_abcdefghijkl"
IDEMPOTENCY_KEY = "frontend_request_abcdefghijkl"
FRONTEND_PNG = make_png(320, 240)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("session_start", {"projects:read", "frontend:interact", "shell:execute"}),
        ("snapshot", {"projects:read", "frontend:read"}),
        ("inspect", {"projects:read", "frontend:read"}),
        ("source", {"projects:read", "frontend:read", "files:read"}),
        ("act", {"projects:read", "frontend:interact"}),
        ("session_stop", {"projects:read", "frontend:interact"}),
    ],
)
def test_frontend_operation_scope_matrix(operation: str, expected: set[str]) -> None:
    assert _required_scopes("project_frontend", operation) == expected


def test_only_start_and_act_are_frontend_replay_unsafe() -> None:
    for operation in ("session_start", "act"):
        assert _is_replay_unsafe("project_frontend", {"operation": operation})
    for operation in ("snapshot", "inspect", "source", "session_stop"):
        assert not _is_replay_unsafe("project_frontend", {"operation": operation})


def test_start_result_accepts_same_origin_redirect_after_url_normalization() -> None:
    arguments = {
        "operation": "session_start",
        "project_id": "project_abcdefghijkl",
        "purpose": "Follow the app login redirect",
        "route": "/dashboard",
        "viewport": {"width": 1440, "height": 900},
        "ready_timeout_seconds": 30,
        "idempotency_key": IDEMPOTENCY_KEY,
    }
    payload = {
        "ok": True,
        "text": "Redirected to login",
        "result": {
            "operation": "session_start",
            "project_id": arguments["project_id"],
            "session_id": SESSION_ID,
            "base_url": "http://LOCALHOST.:80/",
            "url": "http://localhost/login",
            "viewport": arguments["viewport"],
            "dev_server": {"ownership": "reused", "health": "ready"},
        },
    }
    assert (
        _validate_device_result_for_request("project_frontend", arguments, payload)["result"]["url"]
        == "http://localhost/login"
    )


@override_settings(OPERATION_TIMEOUT_SECONDS=45, FRONTEND_START_TIMEOUT_SECONDS=180)
@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_frontend_start_gets_budget_for_approval_and_readiness(device, link) -> None:
    device.status = Device.Status.ONLINE
    device.connection_epoch = 30
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Frontend start budget",
        root_fingerprint="9" * 64,
    )

    async def handler(raw):
        envelope = TunnelEnvelope.model_validate(raw)
        assert envelope.deadline_at is not None
        assert 290 < (envelope.deadline_at - envelope.sent_at).total_seconds() <= 300
        return {
            "ok": True,
            "text": "Frontend session ready",
            "result": {
                "operation": "session_start",
                "project_id": project.pk,
                "session_id": SESSION_ID,
                "base_url": "http://127.0.0.1:5173",
                "url": "http://127.0.0.1:5173/login",
                "viewport": {"width": 1440, "height": 900},
                "dev_server": {"ownership": "managed", "health": "ready"},
            },
        }

    principal = replace(
        principal_for(device, link),
        scopes=frozenset({"projects:read", "frontend:interact", "shell:execute"}),
    )
    receipt = await dispatch_tool(
        principal,
        "project_frontend",
        {
            "operation": "session_start",
            "project_id": project.pk,
            "purpose": "Start a slow local dev server",
            "route": "/demos/dashboard",
            "ready_timeout_seconds": 120,
            "idempotency_key": "frontend_start_budget_key",
        },
        transport=InMemoryTransport(handler),
    )
    assert receipt.result["result"]["session_id"] == SESSION_ID
    assert receipt.result["result"]["url"] == "http://127.0.0.1:5173/login"


@pytest.mark.asyncio
async def test_hidden_frontend_alias_forwards_flat_inspect_target(monkeypatch) -> None:
    from codito_relay.core import mcp_server

    captured: list[tuple[str, dict[str, object]]] = []

    async def run(_context, name, arguments):
        captured.append((name, arguments))
        return "synthetic result"

    monkeypatch.setattr(mcp_server, "_run", run)
    result = await mcp_server.project_frontend(
        None,
        "inspect",
        "project_abcdefghijkl",
        "Inspect the selected control",
        session_id=SESSION_ID,
        snapshot_id=SNAPSHOT_ID,
        element_id="e1",
    )
    assert result == "synthetic result"
    assert captured == [
        (
            "project_frontend",
            {
                "operation": "inspect",
                "project_id": "project_abcdefghijkl",
                "purpose": "Inspect the selected control",
                "session_id": SESSION_ID,
                "snapshot_id": SNAPSHOT_ID,
                "element_id": "e1",
            },
        )
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_frontend_snapshot_is_project_bound_image_content_and_not_durable(device, link):
    device.status = Device.Status.ONLINE
    device.connection_epoch = 31
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Frontend snapshot",
        root_fingerprint="f" * 64,
    )

    async def handler(raw):
        envelope = TunnelEnvelope.model_validate(raw)
        assert envelope.bindings.project_id == project.pk
        assert envelope.payload["tool_name"] == "project_frontend"
        assert envelope.payload["input"] == {
            "operation": "snapshot",
            "project_id": project.pk,
            "session_id": SESSION_ID,
            "purpose": "Review the rendered page",
            "max_elements": 20,
        }
        return {
            "ok": True,
            "text": "Frontend snapshot captured",
            "result": {
                "operation": "snapshot",
                "project_id": project.pk,
                "session_id": SESSION_ID,
                "snapshot_id": SNAPSHOT_ID,
                "url": "http://127.0.0.1:4173/",
                "title": "Local app",
                "viewport": {"width": 320, "height": 240},
                "elements": [
                    {
                        "element_id": "e1",
                        "tag": "button",
                        "role": "button",
                        "name": "Save",
                        "text": "Save",
                        "box": {"x": 10, "y": 20, "width": 80, "height": 32},
                        "visible": True,
                    }
                ],
                "console": [],
                "network": [],
                "truncated": False,
                "mime_type": "image/png",
                "width": 320,
                "height": 240,
                "captured_at": timezone.now().isoformat(),
                "sha256": hashlib.sha256(base64.b64decode(FRONTEND_PNG)).hexdigest(),
                "image_base64": FRONTEND_PNG,
            },
        }

    principal = replace(
        principal_for(device, link), scopes=frozenset({"projects:read", "frontend:read"})
    )
    receipt = await dispatch_tool(
        principal,
        "project_frontend",
        {
            "operation": "snapshot",
            "project_id": project.pk,
            "session_id": SESSION_ID,
            "purpose": "Review the rendered page",
            "max_elements": 20,
        },
        transport=InMemoryTransport(handler),
    )
    rendered = success_tool_result(receipt)
    assert rendered["content"][1] == {
        "type": "image",
        "data": FRONTEND_PNG,
        "mimeType": "image/png",
    }
    assert rendered["structuredContent"]["result"]["snapshot_id"] == SNAPSHOT_ID
    assert "image_base64" not in rendered["structuredContent"]["result"]
    operation = await sync_to_async(Operation.objects.get)(pk=receipt.operation_id)
    assert FRONTEND_PNG not in str(operation.result)
    assert "image_base64" not in str(operation.result)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {
            "operation": "session_start",
            "purpose": "Start local UI review",
            "route": "/",
            "idempotency_key": IDEMPOTENCY_KEY,
        },
        {
            "operation": "act",
            "purpose": "Exercise the save control",
            "session_id": SESSION_ID,
            "snapshot_id": SNAPSHOT_ID,
            "element_id": "e1",
            "action": "click",
            "idempotency_key": IDEMPOTENCY_KEY,
        },
    ],
)
async def test_frontend_replay_unsafe_transport_failure_is_outcome_unknown(device, link, arguments):
    device.status = Device.Status.ONLINE
    device.connection_epoch = 32
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account,
        device=device,
        title="Uncertain frontend operation",
        root_fingerprint="a" * 64,
    )

    async def handler(_raw):
        raise ToolDispatchError("operation_timeout", "Synthetic timeout", retryable=True)

    principal = replace(
        principal_for(device, link),
        scopes=frozenset({"projects:read", "frontend:interact", "shell:execute"}),
    )
    with pytest.raises(ToolDispatchError) as caught:
        await dispatch_tool(
            principal,
            "project_frontend",
            {"project_id": project.pk, **arguments},
            transport=InMemoryTransport(handler),
        )
    assert caught.value.code == "outcome_unknown"
    assert caught.value.retryable is False
    operation = await sync_to_async(Operation.objects.get)(kind=Operation.Kind.FRONTEND)
    assert operation.project_id == project.pk
    assert operation.status == Operation.Status.OUTCOME_UNKNOWN
    assert operation.error_code == "outcome_unknown"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["session_start", "act"])
def test_reconnect_never_replays_uncertain_frontend_mutations(device, link, operation) -> None:
    device.connection_epoch = 41
    device.status = Device.Status.ONLINE
    device.save(update_fields=["connection_epoch", "status"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Reconnect frontend",
        root_fingerprint="b" * 64,
    )
    request_input = {
        "operation": operation,
        "project_id": project.pk,
        "purpose": "Reconnect safety test",
        "idempotency_key": f"frontend_reconnect_{operation}_key",
    }
    if operation == "session_start":
        request_input["route"] = "/"
    else:
        request_input.update(
            {
                "session_id": SESSION_ID,
                "snapshot_id": SNAPSHOT_ID,
                "element_id": "e1",
                "action": "click",
            }
        )
    request_payload = {"tool_name": "project_frontend", "input": request_input}
    operation_row = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.FRONTEND,
        status=Operation.Status.RUNNING,
        connection_epoch=40,
        action_digest=compute_action_digest(request_payload),
        request_digest="c" * 64,
        request_payload=request_payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
        delivery_attempted_at=timezone.now(),
        sent_at=timezone.now(),
        sent_epoch=40,
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=41
    )

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send_json(self, value: dict[str, object]) -> None:
            self.sent.append(value)

    class Redis:
        def __init__(self) -> None:
            self.results: list[dict[str, str]] = []

        async def xadd(self, _stream: str, value: dict[str, str], **_kwargs: object) -> None:
            self.results.append(value)

        async def expire(self, _stream: str, _seconds: int) -> None:
            return None

    socket = Socket()
    redis = Redis()
    async_to_sync(_recover_unsent_operations)(socket, redis, connection)
    operation_row.refresh_from_db()
    assert socket.sent == []
    assert operation_row.status == Operation.Status.OUTCOME_UNKNOWN
    assert operation_row.error_code == "outcome_unknown"
    assert operation_row.result is None
    assert len(redis.results) == 1


@pytest.mark.django_db(transaction=True)
def test_invalid_frontend_act_terminal_is_persisted_as_outcome_unknown(device, link) -> None:
    device.connection_epoch = 51
    device.status = Device.Status.ONLINE
    device.save(update_fields=["connection_epoch", "status"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Invalid frontend terminal",
        root_fingerprint="d" * 64,
    )
    request_payload = {
        "tool_name": "project_frontend",
        "input": {
            "operation": "act",
            "project_id": project.pk,
            "purpose": "Validate uncertain terminal handling",
            "session_id": SESSION_ID,
            "snapshot_id": SNAPSHOT_ID,
            "element_id": "e1",
            "action": "click",
            "idempotency_key": IDEMPOTENCY_KEY,
        },
    }
    digest = compute_action_digest(request_payload)
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.FRONTEND,
        status=Operation.Status.RUNNING,
        connection_epoch=51,
        action_digest=digest,
        request_digest="e" * 64,
        request_payload=request_payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=51
    )
    terminal = TunnelEnvelope(
        kind=MessageKind.OPERATION_RESULT,
        message_id="frontend_terminal_abcdefghijkl",
        correlation_id=str(operation.correlation_id),
        sequence=1,
        connection_epoch=51,
        bindings=TunnelBindings(
            account_id=f"account_{device.account_id:016x}",
            device_id=str(device.pk),
            link_id=str(link.link_id),
            grant_id=operation.oauth_grant_id,
            project_id=project.pk,
        ),
        action_digest=digest,
        payload={"ok": True, "text": "Malformed success", "result": {"operation": "act"}},
    )
    _record_device_message(connection, terminal)
    operation.refresh_from_db()
    assert operation.status == Operation.Status.OUTCOME_UNKNOWN
    assert operation.error_code == "outcome_unknown"
    assert operation.result["ok"] is False
    assert terminal.payload["error"]["code"] == "outcome_unknown"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("requested_operation", "result_payload", "expected_status", "expected_error"),
    [
        (
            "act",
            {
                "operation": "act",
                "project_id": "PROJECT",
                "session_id": "frontend_session_substituted",
                "snapshot_id": SNAPSHOT_ID,
                "element_id": "e1",
                "action": "click",
                "status": "completed",
                "snapshot_invalidated": True,
            },
            Operation.Status.OUTCOME_UNKNOWN,
            "outcome_unknown",
        ),
        (
            "inspect",
            {
                "operation": "inspect",
                "project_id": "PROJECT",
                "session_id": SESSION_ID,
                "snapshot_id": SNAPSHOT_ID,
                "element": {
                    "element_id": "e2",
                    "tag": "button",
                    "box": {"x": 0, "y": 0, "width": 80, "height": 32},
                    "visible": True,
                },
                "accessibility": {},
            },
            Operation.Status.FAILED,
            "internal_error",
        ),
    ],
)
def test_request_mismatched_frontend_terminal_is_discarded_before_persistence(
    device, link, requested_operation, result_payload, expected_status, expected_error
) -> None:
    device.connection_epoch = 52
    device.status = Device.Status.ONLINE
    device.save(update_fields=["connection_epoch", "status"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Substituted frontend terminal",
        root_fingerprint="e" * 64,
    )
    request_input = {
        "operation": requested_operation,
        "project_id": project.pk,
        "purpose": "Reject a request-mismatched result",
        "session_id": SESSION_ID,
        "snapshot_id": SNAPSHOT_ID,
        "element_id": "e1",
    }
    if requested_operation == "act":
        request_input.update({"action": "click", "idempotency_key": IDEMPOTENCY_KEY})
    request_payload = {"tool_name": "project_frontend", "input": request_input}
    digest = compute_action_digest(request_payload)
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.FRONTEND,
        status=Operation.Status.RUNNING,
        connection_epoch=52,
        action_digest=digest,
        request_digest="f" * 64,
        request_payload=request_payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=52
    )
    terminal = TunnelEnvelope(
        kind=MessageKind.OPERATION_RESULT,
        message_id=f"frontend_substitution_{requested_operation}",
        correlation_id=str(operation.correlation_id),
        sequence=1,
        connection_epoch=52,
        bindings=TunnelBindings(
            account_id=f"account_{device.account_id:016x}",
            device_id=str(device.pk),
            link_id=str(link.link_id),
            grant_id=operation.oauth_grant_id,
            project_id=project.pk,
        ),
        action_digest=digest,
        payload={
            "ok": True,
            "text": "Valid shape with substituted request identity",
            "result": {
                **result_payload,
                "project_id": project.pk,
            },
        },
    )

    _record_device_message(connection, terminal)

    operation.refresh_from_db()
    assert operation.status == expected_status
    assert operation.error_code == expected_error
    assert operation.result["ok"] is False
    assert terminal.payload["error"]["code"] == expected_error


@pytest.mark.django_db(transaction=True)
def test_read_terminal_substitution_is_discarded_before_persistence(device, link) -> None:
    device.connection_epoch = 53
    device.status = Device.Status.ONLINE
    device.save(update_fields=["connection_epoch", "status"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Substituted read terminal",
        root_fingerprint="1" * 64,
    )
    request_payload = {
        "tool_name": "project_read",
        "input": {
            "operation": "read_file",
            "project_id": project.pk,
            "path": "README.md",
            "purpose": "Read the requested file",
        },
    }
    digest = compute_action_digest(request_payload)
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.READ,
        status=Operation.Status.RUNNING,
        connection_epoch=53,
        action_digest=digest,
        request_digest="2" * 64,
        request_payload=request_payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=53
    )
    terminal = TunnelEnvelope(
        kind=MessageKind.OPERATION_RESULT,
        message_id="read_substitution_terminal",
        correlation_id=str(operation.correlation_id),
        sequence=1,
        connection_epoch=53,
        bindings=TunnelBindings(
            account_id=f"account_{device.account_id:016x}",
            device_id=str(device.pk),
            link_id=str(link.link_id),
            grant_id=operation.oauth_grant_id,
            project_id=project.pk,
        ),
        action_digest=digest,
        payload={
            "ok": True,
            "text": "Substituted file result",
            "result": {
                "operation": "read_file",
                "project_id": project.pk,
                "path": "secrets.txt",
                "numbered_text": "1: substituted",
                "encoding": "utf-8",
                "newline": "none",
                "size": 11,
                "sha256": "0" * 64,
                "first_line": 1,
                "last_line": 11,
                "truncated": False,
            },
        },
    )

    _record_device_message(connection, terminal)

    operation.refresh_from_db()
    assert operation.status == Operation.Status.FAILED
    assert operation.error_code == "internal_error"
    assert operation.result["ok"] is False
    assert "substituted" not in str(operation.result)
