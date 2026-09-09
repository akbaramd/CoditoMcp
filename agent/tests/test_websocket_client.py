from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from codito_protocol import MessageKind, TunnelBindings, TunnelEnvelope, compute_action_digest

from codito_agent.db import AgentDatabase
from codito_agent.models import OperationState
from codito_agent.read_tools import ToolResponse
from codito_agent.websocket_client import DeviceWebSocketClient, WebSocketTicket, _ExecutionBudget


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, value: str) -> None:
        self.sent.append(value)

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


def budget_client(tmp_path, operation):
    database = AgentDatabase(tmp_path / "clock-budget.sqlite")
    ticket = WebSocketTicket(
        "ticket_abcdefghijkl",
        "account_abcdefghijkl",
        "device_abcdefghijkl",
        "link_abcdefghijklmnop",
        "challenge_abcdefgh",
        datetime.now(UTC) + timedelta(minutes=2),
    )

    async def tickets():
        return ticket

    client = DeviceWebSocketClient(
        url="wss://example.test/ws/device",
        database=database,
        credentials=object(),
        ticket_provider=tickets,
        operation_handler=operation,
        project_metadata=lambda: [],
    )
    client._ticket = ticket
    client._epoch = 1
    client._socket = FakeSocket()
    return client


def budget_envelope(client, *, seconds=45, skew=58):
    sent = datetime.now(UTC) + timedelta(seconds=skew)
    action = {"tool_name": "project_read", "input": {"operation": "list_projects"}}
    return TunnelEnvelope(
        kind=MessageKind.OPERATION,
        message_id="message_clock_budget",
        correlation_id="correlation_clock_budget",
        sequence=1,
        connection_epoch=client._epoch,
        sent_at=sent,
        deadline_at=sent + timedelta(seconds=seconds),
        bindings=TunnelBindings(
            account_id=client._ticket.account_id,
            device_id=client._ticket.device_id,
            link_id=client._ticket.link_id,
            grant_id="grant_abcdefghijklmn",
        ),
        action_digest=compute_action_digest(action),
        payload=action,
    )


@pytest.mark.asyncio
async def test_relay_heartbeat_is_acknowledged_without_reconnect(tmp_path: Path) -> None:
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

    async def operation(*_: Any) -> ToolResponse:
        raise AssertionError("heartbeat must not dispatch an operation")

    client = DeviceWebSocketClient(
        url="wss://example.test/ws/device",
        database=database,
        credentials=object(),  # type: ignore[arg-type]
        ticket_provider=tickets,
        operation_handler=operation,  # type: ignore[arg-type]
        project_metadata=lambda: [],
    )
    socket = FakeSocket()
    client._socket = socket
    client._ticket = ticket
    client._epoch = 9
    incoming = TunnelEnvelope(
        kind=MessageKind.HEARTBEAT,
        message_id="heartbeat_abcdefghijkl",
        sequence=4,
        connection_epoch=9,
        bindings=TunnelBindings(
            account_id=ticket.account_id,
            device_id=ticket.device_id,
            link_id=ticket.link_id,
        ),
        payload={},
    )

    await client._receive(incoming.model_dump_json())

    assert not client._reconnect.is_set()
    assert not hasattr(socket, "closed")
    response = TunnelEnvelope.model_validate_json(socket.sent[-1])
    assert response.kind is MessageKind.HEARTBEAT_ACK
    assert response.correlation_id == incoming.message_id
    assert response.connection_epoch == 9


@pytest.mark.asyncio
async def test_project_metadata_can_be_resynchronized_without_reconnect(tmp_path: Path) -> None:
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

    async def operation(*_: Any) -> ToolResponse:
        raise AssertionError("No operation should be dispatched")

    project_metadata = [{"project_id": "project_abcdefghijkl", "title": "Example"}]
    client = DeviceWebSocketClient(
        url="wss://example.test/ws/device",
        database=database,
        credentials=object(),  # type: ignore[arg-type]
        ticket_provider=tickets,
        operation_handler=operation,  # type: ignore[arg-type]
        project_metadata=lambda: project_metadata,
    )
    socket = FakeSocket()
    client._socket = socket
    client._ticket = ticket
    client._epoch = 4

    assert client.connection_epoch == 4
    assert client.last_disconnect_reason is None
    await client.sync_projects()

    envelope = TunnelEnvelope.model_validate_json(socket.sent[0])
    assert envelope.kind is MessageKind.HELLO
    assert envelope.payload == {"projects": project_metadata}

    await client.reconnect()
    assert socket.closed == (1012, "local reconnect requested")
    assert client._reconnect.is_set()


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


@pytest.mark.asyncio
async def test_reconnected_duplicate_read_is_rerun_and_terminalized(tmp_path: Path) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    ticket = WebSocketTicket(
        "ticket_abcdefghijkl",
        "account_abcdefghijkl",
        "device_abcdefghijkl",
        "link_abcdefghijklmnop",
        "challenge_abcdefgh",
        datetime.now(UTC) + timedelta(minutes=1),
    )
    action = {"tool_name": "project_read", "input": {"operation": "list_projects"}}
    digest = compute_action_digest(action)
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    incoming = TunnelEnvelope(
        kind=MessageKind.OPERATION,
        message_id="message_replayed_read",
        correlation_id="correlation_replayed_read",
        sequence=1,
        connection_epoch=2,
        deadline_at=deadline,
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
        correlation_id=incoming.correlation_id or "",
        account_id=ticket.account_id,
        grant_id="grant_abcdefghijklmn",
        link_id=ticket.link_id,
        device_id=ticket.device_id,
        project_id=None,
        capability="project_read",
        action_digest=digest,
        idempotency_key=None,
        request_digest=digest,
        connection_epoch=1,
        deadline_at=incoming.deadline_at.isoformat() if incoming.deadline_at else "",
    )
    calls = 0

    async def operation(
        tool: str,
        payload: dict[str, Any],
        grant: str,
        link: str,
        epoch: int,
        operation_deadline: datetime,
        reconcile_duplicate: bool,
    ) -> ToolResponse:
        nonlocal calls
        calls += 1
        assert reconcile_duplicate
        return ToolResponse({"operation": "list_projects", "projects": []}, "No projects.")

    async def tickets() -> WebSocketTicket:
        return ticket

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
    client._epoch = 2
    await client._receive(incoming.model_dump_json())
    await client._operation_tasks[incoming.message_id]

    assert calls == 1
    stored = database.get_operation(incoming.message_id)
    assert stored is not None and stored["state"] == OperationState.SUCCEEDED.value
    outgoing = [TunnelEnvelope.model_validate_json(value) for value in socket.sent]
    assert [item.kind for item in outgoing] == [
        MessageKind.OPERATION_RECEIVED,
        MessageKind.OPERATION_STARTED,
        MessageKind.OPERATION_RESULT,
    ]
    assert outgoing[0].payload == {"duplicate": True}


@pytest.mark.asyncio
async def test_reconnected_duplicate_shell_start_is_never_replayed(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    ticket = WebSocketTicket(
        "ticket_abcdefghijkl",
        "account_abcdefghijkl",
        "device_abcdefghijkl",
        "link_abcdefghijklmnop",
        "challenge_abcdefgh",
        datetime.now(UTC) + timedelta(minutes=1),
    )
    shell_input = {
        "action": "start",
        "project_id": project.project_id,
        "working_directory": "",
        "purpose": "Must not replay",
        "timeout_seconds": 30,
        "output_limit_bytes": 1024,
        "idempotency_key": "shell_reconnect_key",
        "command": {"kind": "exec", "executable": "cmd.exe", "arguments": ["/c", "echo"]},
    }
    action = {"tool_name": "project_shell", "input": shell_input}
    digest = compute_action_digest(action)
    incoming = TunnelEnvelope(
        kind=MessageKind.OPERATION,
        message_id="message_replayed_shell",
        correlation_id="correlation_replayed_shell",
        sequence=1,
        connection_epoch=2,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        bindings=TunnelBindings(
            account_id=ticket.account_id,
            device_id=ticket.device_id,
            link_id=ticket.link_id,
            grant_id="grant_abcdefghijklmn",
            project_id=project.project_id,
        ),
        action_digest=digest,
        payload=action,
    )
    database.record_received(
        operation_id=incoming.message_id,
        correlation_id=incoming.correlation_id or "",
        account_id=ticket.account_id,
        grant_id="grant_abcdefghijklmn",
        link_id=ticket.link_id,
        device_id=ticket.device_id,
        project_id=project.project_id,
        capability="project_shell",
        action_digest=digest,
        idempotency_key="shell_reconnect_key",
        request_digest=digest,
        connection_epoch=1,
        deadline_at=incoming.deadline_at.isoformat() if incoming.deadline_at else "",
    )

    async def operation(
        tool: str,
        payload: dict[str, Any],
        grant: str,
        link: str,
        epoch: int,
        operation_deadline: datetime,
        reconcile_duplicate: bool,
    ) -> ToolResponse:
        raise AssertionError("uncertain shell start must never be dispatched")

    async def tickets() -> WebSocketTicket:
        return ticket

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
    client._epoch = 2
    await client._receive(incoming.model_dump_json())

    stored = database.get_operation(incoming.message_id)
    assert stored is not None and stored["state"] == OperationState.OUTCOME_UNKNOWN.value
    assert stored["result"]["error"]["code"] == "outcome_unknown"
    outgoing = [TunnelEnvelope.model_validate_json(value) for value in socket.sent]
    assert [item.kind for item in outgoing] == [
        MessageKind.OPERATION_RECEIVED,
        MessageKind.OPERATION_RESULT,
    ]


@pytest.mark.parametrize("skew,expected_seconds", [(58, 45), (0, 45), (-10, 35)])
def test_clock_translation_never_extends_wire_duration(tmp_path, skew, expected_seconds):
    client = budget_client(tmp_path, None)
    envelope = budget_envelope(client, skew=skew)
    received_at = envelope.sent_at - timedelta(seconds=skew)
    original = envelope.model_dump_json()
    budget = _ExecutionBudget.from_envelope(envelope, received_at, 100.0)
    assert budget.local_deadline == received_at + timedelta(seconds=expected_seconds)
    assert budget.monotonic_deadline == 100.0 + expected_seconds
    assert envelope.model_dump_json() == original


@pytest.mark.asyncio
async def test_skewed_clock_handler_gets_local_deadline_but_journal_preserves_wire(tmp_path):
    deadlines = []

    async def operation(_tool, _input, _grant, _link, _epoch, deadline, _reconcile):
        deadlines.append(deadline)
        return ToolResponse({}, "Synthetic result")

    client = budget_client(tmp_path, operation)
    envelope = budget_envelope(client)
    original = envelope.model_dump_json()
    before = datetime.now(UTC)
    await client._receive(original)
    await asyncio.gather(*client._pending)
    assert before + timedelta(seconds=44) <= deadlines[0] <= before + timedelta(seconds=46)
    stored = client.database.get_operation(envelope.message_id)
    assert stored["deadline_at"] == envelope.deadline_at.isoformat()
    assert stored["action_digest"] == envelope.action_digest
    assert envelope.model_dump_json() == original
    assert client._operation_budgets == {}


@pytest.mark.asyncio
async def test_monotonic_timeout_closes_pending_approval_and_survives_clock_rollback(
    tmp_path, monkeypatch
):
    from codito_agent import websocket_client
    from codito_agent.approvals import ApprovalManager, ApprovalRisk
    from codito_agent.ipc import QueuedApprovalPrompt

    queue = QueuedApprovalPrompt()
    real_datetime = datetime
    entered = []

    class BackwardClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) - timedelta(hours=1)

    async def operation(_tool, _input, grant, link, epoch, deadline, _reconcile):
        entered.append(True)
        monkeypatch.setattr(websocket_client, "datetime", BackwardClock)
        await queue(
            ApprovalManager.build_request(
                account_id="account",
                grant_id=grant,
                link_id=link,
                device_id="device",
                project_id="project",
                project_title="Synthetic",
                capability="device:read",
                action_digest="a" * 64,
                connection_epoch=epoch,
                deadline_at=deadline,
                risk=ApprovalRisk.READ,
                summary="Synthetic timeout; no real approval",
            )
        )
        pytest.fail("Unanswered approval cannot complete")

    client = budget_client(tmp_path, operation)
    # Give the Windows runner enough scheduling headroom to enter the handler;
    # the assertion is about monotonic expiry after entry, not sub-second startup.
    incoming = budget_envelope(client, seconds=5.0)
    started = asyncio.get_running_loop().time()
    await client._receive(incoming.model_dump_json())
    await asyncio.gather(*client._pending)
    assert asyncio.get_running_loop().time() - started < 15.0
    assert entered == [True]
    assert queue.pending_count == 0
    result = client.database.get_operation(incoming.message_id)
    assert result["result"]["error"]["code"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_ack_queue_delay_consumes_original_budget_without_dispatch(tmp_path):
    async def operation(*_args):
        pytest.fail("Expired queued operation must not invoke the handler")

    class SlowAck(FakeSocket):
        async def send(self, value):
            if json.loads(value)["kind"] == "operation_received":
                await asyncio.sleep(0.5)
            await super().send(value)

    client = budget_client(tmp_path, operation)
    client._socket = SlowAck()
    envelope = budget_envelope(client, seconds=0.4)
    await client._receive(envelope.model_dump_json())
    await asyncio.gather(*client._pending)
    assert (
        client.database.get_operation(envelope.message_id)["result"]["error"]["code"]
        == "deadline_exceeded"
    )


@pytest.mark.asyncio
async def test_reconnect_duplicate_does_not_reset_first_monotonic_budget(tmp_path):
    entered = asyncio.Event()
    calls = 0

    async def operation(*_args):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    client = budget_client(tmp_path, operation)
    # The reconnect invariant is object continuity, not a wall-clock race. Use a
    # normal budget and cancel the synthetic never-ending handler after the check.
    envelope = budget_envelope(client)
    await client._receive(envelope.model_dump_json())
    await asyncio.wait_for(entered.wait(), timeout=10)
    first = client._operation_budgets[envelope.message_id]
    await asyncio.sleep(0.02)
    client._epoch = 2
    resent = envelope.model_copy(update={"connection_epoch": 2, "sequence": 2})
    await client._receive(resent.model_dump_json())
    assert client._operation_budgets[envelope.message_id] == first
    assert calls == 1
    active = client._operation_tasks[envelope.message_id]
    active.cancel()
    await asyncio.gather(active, return_exceptions=True)


@pytest.mark.parametrize("original_receipt", ["not-a-date", "2026-09-08T12:00:00"])
def test_invalid_original_clock_evidence_fails_closed(tmp_path, original_receipt):
    client = budget_client(tmp_path, None)
    envelope = budget_envelope(client)
    now = datetime.now(UTC)
    budget = _ExecutionBudget.from_envelope(
        envelope,
        now,
        100.0,
        original_received_at=original_receipt,
    )
    assert budget.local_deadline == now and budget.monotonic_deadline == 100.0


def test_crash_recovery_anchors_to_original_local_receipt_not_retry_time(tmp_path):
    client = budget_client(tmp_path, None)
    envelope = budget_envelope(client)
    received_at = envelope.sent_at - timedelta(seconds=58)
    budget = _ExecutionBudget.from_envelope(
        envelope,
        received_at + timedelta(seconds=40),
        200.0,
        original_received_at=received_at.isoformat(),
    )
    assert budget.local_deadline == received_at + timedelta(seconds=45)
    assert budget.monotonic_deadline == 205.0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["device_desktop", "project_shell"])
@pytest.mark.parametrize("blocking", [False, True])
async def test_native_dispatch_timeout_is_uncertain_and_not_replayed(
    tmp_path, tool, blocking, monkeypatch
):
    from codito_agent import websocket_client

    started = []
    real_datetime = datetime

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(hours=1)

    async def operation(*_args):
        started.append(True)
        if blocking:
            # Model a synchronous native call that returns only after the local
            # response budget has elapsed, without depending on runner timing.
            monkeypatch.setattr(websocket_client, "datetime", FutureClock)
            return ToolResponse({}, "OS may already have accepted the action")
        # Model the asyncio timeout branch after the irreversible handler boundary.
        raise TimeoutError

    client = budget_client(tmp_path, operation)
    envelope = budget_envelope(client)
    payload = {"url": "https://example.com/", "purpose": "Synthetic timeout"}
    bindings = envelope.bindings
    if tool == "project_shell":
        root = tmp_path / "root"
        root.mkdir()
        project = client.database.register_project("Synthetic", root)
        bindings = bindings.model_copy(update={"project_id": project.project_id})
        payload = {
            "action": "start",
            "project_id": project.project_id,
            "working_directory": "",
            "purpose": "Synthetic timeout",
            "timeout_seconds": 30,
            "output_limit_bytes": 1024,
            "idempotency_key": "synthetic_timeout_shell",
            "command": {"kind": "exec", "executable": "cmd.exe", "arguments": ["/c", "echo"]},
        }
    action = {"tool_name": tool, "input": payload}
    envelope = envelope.model_copy(
        update={
            "payload": action,
            "action_digest": compute_action_digest(action),
            "bindings": bindings,
        }
    )
    await client._receive(envelope.model_dump_json())
    await asyncio.gather(*client._pending)
    assert started == [True]
    stored = client.database.get_operation(envelope.message_id)
    assert stored["state"] == OperationState.OUTCOME_UNKNOWN.value
    assert stored["result"]["error"]["code"] == "outcome_unknown"
    assert stored["result"]["error"]["retryable"] is False
    monkeypatch.setattr(websocket_client, "datetime", real_datetime)
    client._epoch = 2
    resent = envelope.model_copy(update={"connection_epoch": 2, "sequence": 2})
    await client._receive(resent.model_dump_json())
    assert started == [True]


@pytest.mark.asyncio
async def test_native_timeout_before_handler_is_not_marked_uncertain(tmp_path):
    async def operation(*_args):
        pytest.fail("Native handler must not start after blocked dispatch notification")

    class SlowStart(FakeSocket):
        async def send(self, value):
            if json.loads(value)["kind"] == "operation_started":
                await asyncio.sleep(1)
            await super().send(value)

    client = budget_client(tmp_path, operation)
    client._socket = SlowStart()
    envelope = budget_envelope(client, seconds=0.5)
    action = {
        "tool_name": "device_desktop",
        "input": {
            "url": "https://example.com/",
            "purpose": "Synthetic timeout",
        },
    }
    envelope = envelope.model_copy(
        update={"payload": action, "action_digest": compute_action_digest(action)}
    )
    await client._receive(envelope.model_dump_json())
    await asyncio.gather(*client._pending)
    stored = client.database.get_operation(envelope.message_id)
    assert stored["state"] == OperationState.FAILED.value
    assert stored["result"]["error"]["code"] == "deadline_exceeded"


def test_frontend_native_replay_classification_is_narrow() -> None:
    from codito_agent.websocket_client import _is_uncertain_native_action

    assert _is_uncertain_native_action("project_frontend", {"operation": "session_start"})
    assert _is_uncertain_native_action("project_frontend", {"operation": "act"})
    for operation in ("snapshot", "inspect", "source", "session_stop"):
        assert not _is_uncertain_native_action("project_frontend", {"operation": operation})
