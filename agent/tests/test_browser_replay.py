"""Browser side effects must never replay after an uncertain agent restart."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from codito_protocol import MessageKind, TunnelBindings, TunnelEnvelope, compute_action_digest
from test_websocket_client import FakeSocket

from codito_agent.db import AgentDatabase
from codito_agent.models import OperationState
from codito_agent.websocket_client import DeviceWebSocketClient, WebSocketTicket


@pytest.mark.parametrize("prior_state", ["received", "running", "succeeded", "active"])
async def test_duplicate_browser_request_never_launches_a_second_time(tmp_path, prior_state):
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    ticket = WebSocketTicket(
        "ticket_abcdefghijkl",
        "account_abcdefghijkl",
        "device_abcdefghijkl",
        "link_abcdefghijklmnop",
        "challenge_abcdefgh",
        datetime.now(UTC) + timedelta(minutes=1),
    )
    action = {
        "tool_name": "device_desktop",
        "input": {
            "action": "open_browser",
            "browser": "firefox",
            "url": "https://example.com/",
            "purpose": "Never replay browser",
        },
    }
    digest = compute_action_digest(action)
    incoming = TunnelEnvelope(
        kind=MessageKind.OPERATION,
        message_id="message_replayed_browser",
        correlation_id="correlation_replayed_browser",
        sequence=1,
        connection_epoch=2,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        bindings=TunnelBindings(
            account_id=ticket.account_id,
            device_id=ticket.device_id,
            link_id=ticket.link_id,
            grant_id="grant_abcdefghijklmn",
        ),
        action_digest=digest,
        payload=action,
    )
    database.record_received(
        operation_id=incoming.message_id,
        correlation_id=incoming.correlation_id,
        account_id=ticket.account_id,
        grant_id="grant_abcdefghijklmn",
        link_id=ticket.link_id,
        device_id=ticket.device_id,
        project_id=None,
        capability="device_desktop",
        action_digest=digest,
        idempotency_key=None,
        request_digest=digest,
        connection_epoch=1,
        deadline_at=incoming.deadline_at.isoformat(),
    )
    if prior_state == "running":
        database.transition_operation(incoming.message_id, OperationState.RUNNING)
    prior_result = {
        "ok": True,
        "text": "Submitted, page load not confirmed",
        "result": {
            "action": "open_browser",
            "browser": "firefox",
            "url": "https://example.com/",
            "status": "submitted",
        },
    }
    if prior_state == "succeeded":
        database.transition_operation(incoming.message_id, OperationState.SUCCEEDED, prior_result)

    async def tickets():
        return ticket

    async def operation(*args):
        pytest.fail("Duplicate browser command was dispatched again")

    client = DeviceWebSocketClient(
        url="wss://example.test/ws/device",
        database=database,
        credentials=object(),
        ticket_provider=tickets,
        operation_handler=operation,
        project_metadata=lambda: [],
    )
    socket = FakeSocket()
    client._socket = socket
    client._ticket = ticket
    client._epoch = 2
    active = None
    if prior_state == "active":
        active = asyncio.create_task(asyncio.Event().wait())
        client._operation_tasks[incoming.message_id] = active
    try:
        await client._receive(incoming.model_dump_json())
        messages = [TunnelEnvelope.model_validate_json(value) for value in socket.sent]
        assert messages[0].kind is MessageKind.OPERATION_RECEIVED
        assert messages[0].payload == {"duplicate": True}
        assert not any(value.kind is MessageKind.OPERATION_STARTED for value in messages)
        if prior_state == "active":
            assert len(messages) == 1
            return
        assert messages[-1].kind is MessageKind.OPERATION_RESULT
        if prior_state == "succeeded":
            assert messages[-1].payload == prior_result
        else:
            assert messages[-1].payload["error"]["code"] == "outcome_unknown"
            assert not messages[-1].payload["error"]["retryable"]
            stored = database.get_operation(incoming.message_id)
            assert stored["state"] == OperationState.OUTCOME_UNKNOWN.value
    finally:
        if active is not None:
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
