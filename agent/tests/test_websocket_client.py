from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from codito_protocol import MessageKind, TunnelBindings, TunnelEnvelope, compute_action_digest

from codito_agent.db import AgentDatabase
from codito_agent.models import OperationState
from codito_agent.read_tools import ToolResponse
from codito_agent.websocket_client import DeviceWebSocketClient, WebSocketTicket


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, value: str) -> None:
        self.sent.append(value)


@pytest.mark.asyncio
async def test_operation_responses_echo_correlation_not_message_id(tmp_path: Path) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    ticket = WebSocketTicket(
        "ticket_abcdefghijkl",
        "account_abcdefghijkl",
        "device_abcdefghijkl",
        "link_abcdefghijklmnop",
        "challenge_abcdefgh",
        datetime.now(UTC) + timedelta(minutes=1),
    )

    async def tickets() -> WebSocketTicket:
        return ticket

    async def operation(
        tool: str,
        payload: dict[str, Any],
        grant: str,
        link: str,
        epoch: int,
        deadline: datetime,
        reconcile_duplicate: bool,
    ) -> ToolResponse:
        assert tool == "project_read"
        assert link == ticket.link_id
        assert deadline > datetime.now(UTC)
        assert not reconcile_duplicate
        return ToolResponse({"operation": "list_projects", "projects": []}, "No projects.")

    client = DeviceWebSocketClient(
        url="wss://example.test/ws/device",
        database=database,
        credentials=object(),  # type: ignore[arg-type]
        ticket_provider=tickets,
        operation_handler=operation,
        project_metadata=lambda: [],
    )
    socket = FakeSocket()
    client._socket = socket
    client._ticket = ticket
    client._epoch = 7
    action = {"tool_name": "project_read", "input": {"operation": "list_projects"}}
    incoming = TunnelEnvelope(
        kind=MessageKind.OPERATION,
        message_id="message_abcdefghijkl",
        correlation_id="correlation_abcdefgh",
        sequence=1,
        connection_epoch=7,
        sent_at=datetime.now(UTC),
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        bindings=TunnelBindings(
            account_id=ticket.account_id,
            device_id=ticket.device_id,
            link_id=ticket.link_id,
            grant_id="grant_abcdefghijklmn",
        ),
        action_digest=compute_action_digest(action),
        payload=action,
    )
    await client._receive(incoming.model_dump_json())
    await asyncio.gather(*client._pending)
    outgoing = [TunnelEnvelope.model_validate_json(value) for value in socket.sent]
    assert {item.kind for item in outgoing} == {
        MessageKind.OPERATION_RECEIVED,
        MessageKind.OPERATION_STARTED,
        MessageKind.OPERATION_RESULT,
    }
    assert all(item.correlation_id == incoming.correlation_id for item in outgoing)


@pytest.mark.asyncio
async def test_terminal_replay_uses_fresh_epoch_message_and_sequence(tmp_path: Path) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    ticket = WebSocketTicket(
        "ticket_abcdefghijkl",
        "account_abcdefghijkl",
        "device_abcdefghijkl",
        "link_abcdefghijklmnop",
        "challenge_abcdefgh",
        datetime.now(UTC) + timedelta(minutes=1),
    )

    async def tickets() -> WebSocketTicket:
        return ticket

    async def operation(
        tool: str,
        payload: dict[str, Any],
        grant: str,
        link: str,
        epoch: int,
        deadline: datetime,
        reconcile_duplicate: bool,
    ) -> ToolResponse:
        raise AssertionError("not dispatched")

    client = DeviceWebSocketClient(
        url="wss://example.test/ws/device",
        database=database,
        credentials=object(),  # type: ignore[arg-type]
        ticket_provider=tickets,
        operation_handler=operation,
        project_metadata=lambda: [],
    )
    old = TunnelEnvelope(
        kind=MessageKind.OPERATION_RESULT,
        message_id="terminal_message_old",
        correlation_id="correlation_abcdefgh",
        sequence=9,
        connection_epoch=3,
        bindings=TunnelBindings(
            account_id=ticket.account_id,
            device_id=ticket.device_id,
            link_id=ticket.link_id,
            grant_id="grant_abcdefghijklmn",
            project_id="project_abcdefghijkl",
        ),
        action_digest="a" * 64,
        payload={"ok": True, "result": {}},
    )
    client._terminal_pending[old.correlation_id or ""] = old
    socket = FakeSocket()
    client._socket = socket
    client._ticket = ticket
    client._epoch = 4
    client._out_sequence = 1

    await client._replay_terminal(old)
    replayed = TunnelEnvelope.model_validate_json(socket.sent[-1])
    assert replayed.connection_epoch == 4
    assert replayed.sequence == 2
    assert replayed.message_id.startswith("msg_")
    assert replayed.message_id != old.message_id
    assert replayed.correlation_id == old.correlation_id
    assert replayed.action_digest == old.action_digest
    assert client._terminal_pending[old.correlation_id or ""] == replayed


@pytest.mark.asyncio
async def test_terminal_result_is_reconstructed_from_sqlite_after_process_restart(
    tmp_path: Path,
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    values = {
        "operation_id": "message_durable1234",
        "correlation_id": "correlation_durable1234",
        "account_id": "account_abcdefghijkl",
        "grant_id": "grant_abcdefghijklmn",
        "link_id": "link_abcdefghijklmnop",
        "device_id": "device_abcdefghijkl",
        "project_id": None,
        "capability": "project_read",
        "action_digest": "a" * 64,
        "idempotency_key": None,
        "request_digest": "a" * 64,
        "connection_epoch": 3,
        "deadline_at": "2030-01-01T00:00:00+00:00",
    }
    database.record_received(**values)
    database.transition_operation(
        values["operation_id"], OperationState.SUCCEEDED, {"ok": True, "result": {}}
    )
    ticket = WebSocketTicket(
        "ticket_abcdefghijkl",
        values["account_id"],
        values["device_id"],
        "link_current1234567",
        "challenge_abcdefgh",
        datetime.now(UTC) + timedelta(minutes=1),
    )

    async def tickets() -> WebSocketTicket:
        return ticket

    async def operation(
        tool: str,
        payload: dict[str, Any],
        grant: str,
        link: str,
        epoch: int,
        deadline: datetime,
        reconcile_duplicate: bool,
    ) -> ToolResponse:
        raise AssertionError("durable terminal must not be dispatched")

    client = DeviceWebSocketClient(
        url="wss://example.test/ws/device",
        database=database,
        credentials=object(),  # type: ignore[arg-type]
        ticket_provider=tickets,
        operation_handler=operation,
        project_metadata=lambda: [],
    )
    socket = FakeSocket()
    client._socket = socket
    client._ticket = ticket
    client._epoch = 4
    await client._replay_pending_terminals()

    replayed = TunnelEnvelope.model_validate_json(socket.sent[-1])
    assert replayed.connection_epoch == 4
    assert replayed.message_id.startswith("msg_")
    assert replayed.correlation_id == values["correlation_id"]
    assert replayed.bindings.link_id == values["link_id"]
    assert replayed.payload == {"ok": True, "result": {}}

    ack = TunnelEnvelope(
        kind=MessageKind.TERMINAL_ACK,
        message_id="message_ack123456",
        correlation_id=values["correlation_id"],
        sequence=2,
        connection_epoch=4,
        bindings=replayed.bindings,
        action_digest=values["action_digest"],
        payload={},
    )
    await client._receive(ack.model_dump_json())
    assert database.list_unacknowledged_terminals() == []
