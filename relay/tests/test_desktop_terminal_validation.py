"""Validate browser results before real WebSocket persistence and publication."""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from codito_protocol import MessageKind, TunnelBindings, TunnelEnvelope, compute_action_digest
from django.utils import timezone
from starlette.websockets import WebSocketDisconnect

from codito_relay.core.models import Operation
from codito_relay.core.websocket import (
    DeviceConnection,
    _receive_loop,
    _record_device_message,
)


@pytest.fixture
def desktop_operation(device, link):
    device.connection_epoch = 4
    device.save(update_fields=["connection_epoch"])
    request = {
        "tool_name": "device_desktop",
        "input": {
            "action": "open_browser",
            "browser": "firefox",
            "url": "https://example.com/",
            "purpose": "Synthetic terminal validation",
        },
    }
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        kind="device_desktop",
        status=Operation.Status.RUNNING,
        connection_epoch=4,
        action_digest=compute_action_digest(request),
        request_digest="1" * 64,
        request_payload=request,
        deadline_at=timezone.now() + timedelta(minutes=1),
    )
    connection = DeviceConnection(str(device.pk), f"account_{device.account_id:016x}", 4)
    envelope = TunnelEnvelope(
        kind=MessageKind.OPERATION_RESULT,
        message_id="desktop_terminal_12345678",
        correlation_id=str(operation.correlation_id),
        sequence=2,
        connection_epoch=4,
        bindings=TunnelBindings(
            account_id=connection.account_wire_id,
            device_id=str(device.pk),
            grant_id=operation.oauth_grant_id,
            link_id=str(link.link_id),
        ),
        action_digest=operation.action_digest,
        payload={"ok": True, "result": {"url": "https://example.com/", "browser": "firefox"}},
    )
    return operation, connection, envelope


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "result",
    [
        None,
        {"url": "file:///C:/Windows/cmd.exe", "browser": "firefox"},
        {"url": "https://example.com/", "browser": "firefox", "status": "loaded"},
        {"url": "https://another.example/", "browser": "firefox"},
        {"url": "https://example.com/", "browser": "default"},
    ],
)
def test_invalid_browser_terminal_becomes_durable_unknown_not_success(desktop_operation, result):
    operation, connection, envelope = desktop_operation
    envelope.payload = {"ok": True, "text": "Do not trust this success", "result": result}
    original = envelope.model_copy(deep=True)
    persisted = _record_device_message(connection, envelope)
    operation.refresh_from_db()
    assert persisted.status == Operation.Status.OUTCOME_UNKNOWN
    assert operation.status == Operation.Status.OUTCOME_UNKNOWN
    assert operation.error_code == "outcome_unknown"
    assert operation.result == envelope.payload
    assert envelope.payload["ok"] is False
    assert envelope.payload["error"]["code"] == "outcome_unknown"
    assert not envelope.payload["error"]["retryable"]
    assert "Do not trust this success" not in str(operation.result)
    # A replay of the same malformed device result is normalized identically.
    assert _record_device_message(connection, original).result == operation.result


@pytest.mark.django_db(transaction=True)
def test_valid_browser_terminal_is_normalized_and_preserved(desktop_operation):
    operation, connection, envelope = desktop_operation
    _record_device_message(connection, envelope)
    operation.refresh_from_db()
    assert operation.status == Operation.Status.SUCCEEDED
    assert operation.result["result"]["status"] == "submitted"
    assert operation.result["result"]["browser"] == "firefox"
    assert operation.result == envelope.payload


@pytest.mark.django_db(transaction=True)
async def test_receive_loop_publishes_safe_unknown_and_keeps_socket_open(desktop_operation):
    operation, connection, envelope = desktop_operation
    envelope.payload = {"ok": True, "result": {"url": "file:///invalid"}}
    incoming = envelope.model_dump(mode="json")

    class Socket:
        def __init__(self):
            self.received = False
            self.closed = False
            self.sent = []

        async def receive_json(self):
            if self.received:
                raise WebSocketDisconnect()
            self.received = True
            return incoming

        async def send_json(self, value):
            self.sent.append(value)

        async def close(self, **kwargs):
            self.closed = True

    class Redis:
        def __init__(self):
            self.messages = []

        async def xadd(self, stream, fields, **kwargs):
            self.messages.append(fields["envelope"])

        async def expire(self, *args):
            return True

    socket, redis = Socket(), Redis()
    with pytest.raises(WebSocketDisconnect):
        await _receive_loop(socket, redis, connection)
    assert not socket.closed
    assert socket.sent[0]["kind"] == MessageKind.TERMINAL_ACK
    published = TunnelEnvelope.model_validate_json(redis.messages[0])
    assert published.payload["error"]["code"] == "outcome_unknown"
    assert published.payload["error"]["retryable"] is False
    await sync_to_async(operation.refresh_from_db)()
    assert operation.status == Operation.Status.OUTCOME_UNKNOWN
    assert operation.result == published.payload
