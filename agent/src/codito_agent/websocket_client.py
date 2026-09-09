from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from codito_protocol import (
    MessageKind,
    TunnelBindings,
    TunnelEnvelope,
    compute_action_digest,
)
from pydantic import TypeAdapter, ValidationError
from websockets.typing import Subprotocol

from .credentials import DeviceCredentialStore
from .db import AgentDatabase
from .errors import AgentError
from .models import OperationState
from .read_tools import ToolResponse
from .wire_errors import to_tool_failure

logger = logging.getLogger(__name__)
_JITTER = secrets.SystemRandom()


@dataclass(frozen=True, slots=True)
class WebSocketTicket:
    value: str
    account_id: str
    device_id: str
    link_id: str
    challenge: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _ExecutionBudget:
    local_deadline: datetime
    monotonic_deadline: float

    @classmethod
    def from_envelope(
        cls,
        envelope: TunnelEnvelope,
        received_at: datetime,
        received_monotonic: float,
        *,
        original_received_at: str | None = None,
    ) -> _ExecutionBudget:
        """Translate a wire duration, never the host clock, into an execution budget.

        A slow host clock must not turn the relay's 45 seconds into 103 seconds.
        Original journal receipt anchors crash retries; a resend's shorter wire
        duration can reduce that budget but a new local receipt cannot reset it.
        Unknown network transit latency cannot be inferred from two skewed clocks.
        """
        assert envelope.deadline_at is not None
        duration = envelope.deadline_at - envelope.sent_at
        local_deadline = min(envelope.deadline_at, received_at + duration)
        if original_received_at is not None:
            try:
                original = datetime.fromisoformat(original_received_at)
                if original.tzinfo is None:
                    raise ValueError("Missing journal timezone")
                local_deadline = min(local_deadline, original + duration)
            except (TypeError, ValueError):
                local_deadline = received_at  # Invalid clock evidence cannot extend permission.
        return cls(
            local_deadline,
            received_monotonic + (local_deadline - received_at).total_seconds(),
        )


TicketProvider = Callable[[], Awaitable[WebSocketTicket]]
OperationHandler = Callable[
    [str, dict[str, Any], str, str, int, datetime, bool], Awaitable[ToolResponse]
]
ConnectionCallback = Callable[[bool], None]


class DeviceWebSocketClient:
    PING_INTERVAL = 20
    PING_TIMEOUT = 20
    HEARTBEAT_INTERVAL = 30

    def __init__(
        self,
        *,
        url: str,
        database: AgentDatabase,
        credentials: DeviceCredentialStore,
        ticket_provider: TicketProvider,
        operation_handler: OperationHandler,
        project_metadata: Callable[[], list[dict[str, object]]],
        max_pending: int = 32,
        connection_callback: ConnectionCallback | None = None,
    ) -> None:
        self.url = url
        self.database = database
        self.credentials = credentials
        self.ticket_provider = ticket_provider
        self.operation_handler = operation_handler
        self.project_metadata = project_metadata
        self.max_pending = max_pending
        self.connection_callback = connection_callback or (lambda connected: None)
        self._stop = asyncio.Event()
        self._reconnect = asyncio.Event()
        self._socket: Any = None
        self._send_lock = asyncio.Lock()
        self._out_sequence = 0
        self._epoch = 0
        self._ticket: WebSocketTicket | None = None
        self._pending: set[asyncio.Task[None]] = set()
        self._operation_tasks: dict[str, asyncio.Task[None]] = {}
        self._operation_budgets: dict[str, _ExecutionBudget] = {}
        self._terminal_pending: dict[str, TunnelEnvelope] = {}
        self._envelope_adapter = TypeAdapter(TunnelEnvelope)
        self._long_disconnect_task: asyncio.Task[None] | None = None
        self._last_disconnect_reason: str | None = None

    @property
    def connection_epoch(self) -> int:
        return self._epoch

    @property
    def last_disconnect_reason(self) -> str | None:
        return self._last_disconnect_reason

    async def run(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            try:
                ticket = await self.ticket_provider()
                if ticket.expires_at <= datetime.now(UTC) + timedelta(seconds=30):
                    raise AgentError("ticket_expired", "WebSocket ticket is too close to expiry")
                self._ticket = ticket
                signature = self.credentials.websocket_signature(ticket.value, ticket.challenge)
                async with websockets.connect(
                    self.url,
                    additional_headers={
                        "Authorization": f"DeviceTicket {ticket.value}",
                        "X-Codito-Device-Proof": signature,
                    },
                    subprotocols=[Subprotocol("codito.device.v1")],
                    ping_interval=self.PING_INTERVAL,
                    ping_timeout=self.PING_TIMEOUT,
                    max_size=2_500_000,
                    max_queue=32,
                    close_timeout=5,
                ) as socket:
                    self._socket = socket
                    await self._receive_welcome(socket)
                    self.database.set_connection_epoch(self._epoch)
                    self._out_sequence = 0
                    self._last_disconnect_reason = None
                    self.connection_callback(True)
                    if self._long_disconnect_task is not None:
                        self._long_disconnect_task.cancel()
                        self._long_disconnect_task = None
                    await self._hello()
                    await self._replay_pending_terminals()
                    backoff = 1.0
                    heartbeat = asyncio.create_task(self._heartbeat_loop())
                    try:
                        async for raw in socket:
                            await self._receive(raw)
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_disconnect_reason = type(exc).__name__
                logger.warning("device WebSocket disconnected (%s)", type(exc).__name__)
            finally:
                self._socket = None
                self.connection_callback(False)
                if self._long_disconnect_task is None or self._long_disconnect_task.done():
                    self._long_disconnect_task = asyncio.create_task(self._long_disconnect())
            if not self._stop.is_set():
                delay = _JITTER.uniform(0.0, min(backoff, 60.0))
                try:
                    await asyncio.wait_for(self._reconnect.wait(), timeout=delay)
                except TimeoutError:
                    pass
                self._reconnect.clear()
                backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._stop.set()
        self._reconnect.set()
        if self._socket is not None:
            await self._socket.close(code=1000, reason="agent shutdown")
        for task in self._pending:
            task.cancel()
        await asyncio.gather(*self._pending, return_exceptions=True)

    async def reconnect(self) -> None:
        """Interrupt the active socket/backoff so the run loop obtains a fresh fence."""

        self._reconnect.set()
        if self._socket is not None:
            await self._socket.close(code=1012, reason="local reconnect requested")

    async def _hello(self) -> None:
        assert self._ticket is not None
        await self._send_new(
            MessageKind.HELLO,
            payload={"projects": self.project_metadata()},
        )

    async def sync_projects(self) -> None:
        """Publish current local project metadata on an already fenced connection."""

        if self._socket is None or self._ticket is None:
            return
        await self._hello()

    async def _receive_welcome(self, socket: Any) -> None:
        """Accept the relay-authoritative fence before sending any agent data."""

        try:
            raw = await asyncio.wait_for(socket.recv(), 10)
            envelope = self._envelope_adapter.validate_json(raw)
        except (TimeoutError, ValidationError, ValueError) as exc:
            raise AgentError(
                "protocol_error", "Relay did not provide a valid WELCOME fence"
            ) from exc
        if envelope.kind is not MessageKind.WELCOME:
            raise AgentError("protocol_error", "First relay message must be WELCOME")
        assert self._ticket is not None
        if (
            envelope.bindings.account_id != self._ticket.account_id
            or envelope.bindings.device_id != self._ticket.device_id
        ):
            raise AgentError("binding_mismatch", "WELCOME binding does not match the ticket")
        self._epoch = envelope.connection_epoch

    async def _heartbeat_loop(self) -> None:
        while self._socket is not None:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            await self._send_new(
                MessageKind.HEARTBEAT,
                payload={"active_operations": len(self._pending)},
            )

    async def _receive(self, raw: str | bytes) -> None:
        received_at = datetime.now(UTC)
        received_monotonic = asyncio.get_running_loop().time()
        try:
            value = json.loads(raw)
            envelope = self._envelope_adapter.validate_python(value)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            raise AgentError("protocol_error", "Relay sent a malformed tunnel envelope") from exc
        self._validate_bindings(envelope)
        if envelope.kind is MessageKind.TERMINAL_ACK and envelope.correlation_id:
            pending = self._terminal_pending.get(envelope.correlation_id)
            if pending is None:
                return
            if (
                envelope.action_digest != pending.action_digest
                or envelope.bindings != pending.bindings
            ):
                raise AgentError(
                    "binding_mismatch", "Terminal acknowledgement does not match its operation"
                )
            bindings = pending.bindings
            if (
                bindings.grant_id is not None
                and bindings.link_id is not None
                and pending.action_digest is not None
            ):
                self.database.acknowledge_terminal(
                    correlation_id=envelope.correlation_id,
                    action_digest=pending.action_digest,
                    account_id=bindings.account_id,
                    grant_id=bindings.grant_id,
                    link_id=bindings.link_id,
                    device_id=bindings.device_id,
                    project_id=bindings.project_id,
                )
            self._terminal_pending.pop(envelope.correlation_id, None)
            return
        if envelope.kind is MessageKind.HEARTBEAT:
            await self._send_new(
                MessageKind.HEARTBEAT_ACK,
                correlation_id=envelope.message_id,
                payload={},
            )
            return
        if envelope.kind in {MessageKind.HEARTBEAT_ACK, MessageKind.WELCOME}:
            return
        if envelope.kind is MessageKind.OPERATION_CANCEL:
            # Cancellation is expressed through the project_shell cancel tool so it
            # follows the same project/job binding and durable result path.
            return
        if envelope.kind is not MessageKind.OPERATION:
            raise AgentError("protocol_error", "Unexpected relay message kind")
        self._operation_budgets = {
            identifier: budget
            for identifier, budget in self._operation_budgets.items()
            if budget.monotonic_deadline > received_monotonic
            or (
                identifier in self._operation_tasks and not self._operation_tasks[identifier].done()
            )
        }
        if len(self._pending) >= self.max_pending or (
            len(self._operation_budgets) >= self.max_pending
            and envelope.message_id not in self._operation_budgets
        ):
            await self._send_failure(
                envelope, AgentError("queue_full", "Device queue is full", retryable=True)
            )
            return
        if envelope.correlation_id is None:
            await self._send_failure(
                envelope, AgentError("invalid_request", "Operation correlation_id is required")
            )
            return
        if envelope.deadline_at is None or envelope.deadline_at <= datetime.now(UTC):
            await self._send_failure(
                envelope, AgentError("deadline_exceeded", "Operation deadline has elapsed")
            )
            return
        payload = envelope.payload
        tool_name = payload.get("tool_name")
        tool_input = payload.get("input")
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
            await self._send_failure(
                envelope, AgentError("invalid_request", "Tool payload is malformed")
            )
            return
        bindings = envelope.bindings
        input_project_id = tool_input.get("project_id")
        if input_project_id is not None and input_project_id != bindings.project_id:
            await self._send_failure(
                envelope,
                AgentError("binding_mismatch", "Project input does not match the routed binding"),
            )
            return
        action_payload = {"tool_name": tool_name, "input": tool_input}
        if compute_action_digest(action_payload) != envelope.action_digest:
            await self._send_failure(
                envelope, AgentError("action_digest_mismatch", "Action digest does not match input")
            )
            return
        if bindings.grant_id is None or bindings.link_id is None:
            await self._send_failure(
                envelope, AgentError("unauthorized", "OAuth grant or link binding is missing")
            )
            return
        request_digest = compute_action_digest(action_payload)
        inserted = self.database.record_received(
            operation_id=envelope.message_id,
            correlation_id=envelope.correlation_id or envelope.message_id,
            account_id=bindings.account_id,
            grant_id=bindings.grant_id,
            link_id=bindings.link_id,
            device_id=bindings.device_id,
            project_id=bindings.project_id,
            capability=tool_name,
            action_digest=envelope.action_digest,
            idempotency_key=tool_input.get("idempotency_key"),
            request_digest=request_digest,
            request=tool_input,
            connection_epoch=self._epoch,
            deadline_at=envelope.deadline_at.isoformat(),
        )
        await self._send_new(
            MessageKind.OPERATION_RECEIVED,
            correlation_id=envelope.correlation_id,
            payload={"duplicate": not inserted},
            bindings=bindings,
            action_digest=envelope.action_digest,
        )
        budget = _ExecutionBudget.from_envelope(envelope, received_at, received_monotonic)
        if not inserted:
            prior = self.database.get_operation(envelope.message_id)
            if prior and prior.get("result"):
                await self._send_new(
                    MessageKind.OPERATION_RESULT,
                    correlation_id=envelope.correlation_id,
                    payload=prior["result"],
                    bindings=bindings,
                    action_digest=envelope.action_digest,
                    terminal=True,
                )
                return
            active = self._operation_tasks.get(envelope.message_id)
            if active is not None and not active.done():
                # The original coroutine survives transient socket loss and will
                # publish its terminal result using the new connection fence.
                return
            if tool_name == "device_desktop" or (
                tool_name == "project_shell" and tool_input.get("action") == "start"
            ):
                await self._terminalize_uncertain_action(envelope)
                return
            budget = _ExecutionBudget.from_envelope(
                envelope,
                received_at,
                received_monotonic,
                original_received_at=str(prior.get("received_at", "")) if prior else "",
            )
            original_budget = self._operation_budgets.get(envelope.message_id)
            if original_budget is not None:
                budget = _ExecutionBudget(
                    min(budget.local_deadline, original_budget.local_deadline),
                    min(budget.monotonic_deadline, original_budget.monotonic_deadline),
                )
            self._start_dispatch(
                envelope,
                tool_name,
                tool_input,
                bindings.grant_id,
                bindings.link_id,
                budget=budget,
                reconcile_duplicate=True,
            )
            return
        self._start_dispatch(
            envelope,
            tool_name,
            tool_input,
            bindings.grant_id,
            bindings.link_id,
            budget=budget,
            reconcile_duplicate=False,
        )

    def _start_dispatch(
        self,
        envelope: TunnelEnvelope,
        tool_name: str,
        tool_input: dict[str, Any],
        grant_id: str,
        link_id: str,
        *,
        budget: _ExecutionBudget,
        reconcile_duplicate: bool,
    ) -> None:
        self._operation_budgets[envelope.message_id] = budget
        task = asyncio.create_task(
            self._dispatch(
                envelope,
                tool_name,
                tool_input,
                grant_id,
                link_id,
                budget=budget,
                reconcile_duplicate=reconcile_duplicate,
            ),
            name=f"codito-operation-{envelope.message_id}",
        )
        self._pending.add(task)
        self._operation_tasks[envelope.message_id] = task

        def completed(done: asyncio.Task[None]) -> None:
            self._pending.discard(done)
            if self._operation_tasks.get(envelope.message_id) is done:
                self._operation_tasks.pop(envelope.message_id, None)

        task.add_done_callback(completed)

    def _validate_bindings(self, envelope: TunnelEnvelope) -> None:
        if self._ticket is None:
            raise AgentError("protocol_error", "Tunnel has no authenticated ticket")
        if envelope.connection_epoch != self._epoch:
            raise AgentError("stale_connection", "Envelope belongs to a stale connection epoch")
        bindings = envelope.bindings
        if bindings.account_id != self._ticket.account_id:
            raise AgentError("binding_mismatch", "Account binding does not match the ticket")
        if bindings.device_id != self._ticket.device_id:
            raise AgentError("binding_mismatch", "Device binding does not match the ticket")
        if (
            envelope.kind is not MessageKind.TERMINAL_ACK
            and bindings.link_id is not None
            and bindings.link_id != self._ticket.link_id
        ):
            raise AgentError("binding_mismatch", "Link binding does not match the ticket")

    async def _dispatch(
        self,
        envelope: TunnelEnvelope,
        tool_name: str,
        tool_input: dict[str, Any],
        grant_id: str,
        link_id: str,
        *,
        budget: _ExecutionBudget,
        reconcile_duplicate: bool,
    ) -> None:
        self.database.transition_operation(envelope.message_id, OperationState.RUNNING)
        handler_started = False
        native_action = tool_name == "device_desktop" or (
            tool_name == "project_shell" and tool_input.get("action") == "start"
        )

        def deadline_error() -> AgentError:
            if native_action and handler_started:
                return AgentError(
                    "outcome_unknown",
                    "The native action exceeded its response budget and may have executed; "
                    "Codito never automatically replays uncertain actions",
                    retryable=False,
                )
            return AgentError("deadline_exceeded", "Operation execution budget elapsed")

        try:
            async with asyncio.timeout_at(budget.monotonic_deadline):
                if (
                    budget.monotonic_deadline <= asyncio.get_running_loop().time()
                    or budget.local_deadline <= datetime.now(UTC)
                ):
                    raise AgentError(
                        "deadline_exceeded", "Operation budget elapsed before execution"
                    )
                await self._send_new(
                    MessageKind.OPERATION_STARTED,
                    correlation_id=envelope.correlation_id,
                    payload={},
                    bindings=envelope.bindings,
                    action_digest=envelope.action_digest,
                )
                handler_started = True
                response = await self.operation_handler(
                    tool_name,
                    tool_input,
                    grant_id,
                    link_id,
                    self._epoch,
                    budget.local_deadline,
                    reconcile_duplicate,
                )
                if (
                    budget.monotonic_deadline <= asyncio.get_running_loop().time()
                    or budget.local_deadline <= datetime.now(UTC)
                ):
                    raise AgentError(
                        "deadline_exceeded", "Operation budget elapsed during execution"
                    )
            payload = {"ok": True, "result": response.structured, "text": response.text}
            state = OperationState.SUCCEEDED
        except TimeoutError:
            error = deadline_error()
            payload = to_tool_failure(error, envelope.correlation_id).model_dump(
                mode="json", exclude_none=True
            )
            state = (
                OperationState.OUTCOME_UNKNOWN
                if error.code == "outcome_unknown"
                else OperationState.FAILED
            )
        except AgentError as exc:
            if exc.code == "deadline_exceeded":
                exc = deadline_error()
            payload = to_tool_failure(exc, envelope.correlation_id).model_dump(
                mode="json", exclude_none=True
            )
            state = (
                OperationState.DENIED
                if exc.code == "approval_denied"
                else OperationState.OUTCOME_UNKNOWN
                if exc.code == "outcome_unknown"
                else OperationState.FAILED
            )
        except Exception:
            error = AgentError("internal_error", "The device operation failed")
            payload = to_tool_failure(error, envelope.correlation_id).model_dump(
                mode="json", exclude_none=True
            )
            state = OperationState.FAILED
        from codito_protocol.screenshot import durable_tool_result

        self.database.transition_operation(envelope.message_id, state, durable_tool_result(payload))
        self._operation_budgets.pop(envelope.message_id, None)
        await self._send_new(
            MessageKind.OPERATION_RESULT,
            correlation_id=envelope.correlation_id,
            payload=payload,
            bindings=envelope.bindings,
            action_digest=envelope.action_digest,
            terminal=True,
        )

    async def _terminalize_uncertain_action(self, envelope: TunnelEnvelope) -> None:
        error = AgentError(
            "outcome_unknown",
            "A prior native action may have executed; Codito never replays uncertain actions",
            retryable=False,
        )
        payload = to_tool_failure(error, envelope.correlation_id).model_dump(
            mode="json", exclude_none=True
        )
        self.database.transition_operation(
            envelope.message_id, OperationState.OUTCOME_UNKNOWN, payload
        )
        await self._send_new(
            MessageKind.OPERATION_RESULT,
            correlation_id=envelope.correlation_id,
            payload=payload,
            bindings=envelope.bindings,
            action_digest=envelope.action_digest,
            terminal=True,
        )

    async def _send_failure(self, envelope: TunnelEnvelope, error: AgentError) -> None:
        payload = to_tool_failure(error, envelope.correlation_id).model_dump(
            mode="json", exclude_none=True
        )
        await self._send_new(
            MessageKind.OPERATION_RESULT,
            correlation_id=envelope.correlation_id,
            payload=payload,
            bindings=envelope.bindings,
            action_digest=envelope.action_digest,
            terminal=True,
        )

    async def _send_new(
        self,
        kind: MessageKind,
        *,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        bindings: TunnelBindings | None = None,
        action_digest: str | None = None,
        terminal: bool = False,
    ) -> None:
        assert self._ticket is not None
        self._out_sequence += 1
        envelope = TunnelEnvelope(
            kind=kind,
            message_id=f"msg_{secrets.token_urlsafe(24)}",
            correlation_id=correlation_id,
            sequence=self._out_sequence,
            connection_epoch=self._epoch,
            bindings=bindings
            or TunnelBindings(
                account_id=self._ticket.account_id,
                device_id=self._ticket.device_id,
                link_id=self._ticket.link_id,
            ),
            action_digest=action_digest,
            payload=payload,
        )
        if terminal and correlation_id:
            self._terminal_pending[correlation_id] = envelope
        await self._send(envelope)

    async def _send(self, envelope: TunnelEnvelope) -> None:
        if self._socket is None:
            return
        async with self._send_lock:
            await self._socket.send(envelope.model_dump_json(exclude_none=True))

    async def _replay_terminal(self, envelope: TunnelEnvelope) -> None:
        """Re-envelope durable terminal data under the new socket fence."""

        await self._send_new(
            MessageKind.OPERATION_RESULT,
            correlation_id=envelope.correlation_id,
            payload=dict(envelope.payload),
            bindings=envelope.bindings,
            action_digest=envelope.action_digest,
            terminal=True,
        )

    async def _replay_pending_terminals(self) -> None:
        """Rebuild crash-durable results under the current authoritative fence."""

        memory = list(self._terminal_pending.values())
        durable_correlations: set[str] = set()
        for row in self.database.list_unacknowledged_terminals():
            correlation_id = str(row["correlation_id"])
            durable_correlations.add(correlation_id)
            result = row.get("result")
            if not isinstance(result, dict):
                continue
            await self._send_new(
                MessageKind.OPERATION_RESULT,
                correlation_id=correlation_id,
                payload=result,
                bindings=TunnelBindings(
                    account_id=str(row["account_id"]),
                    grant_id=str(row["grant_id"]),
                    link_id=str(row["link_id"]),
                    device_id=str(row["device_id"]),
                    project_id=(str(row["project_id"]) if row["project_id"] else None),
                ),
                action_digest=str(row["action_digest"]),
                terminal=True,
            )
        for envelope in memory:
            if envelope.correlation_id not in durable_correlations:
                await self._replay_terminal(envelope)

    async def _long_disconnect(self) -> None:
        try:
            await asyncio.sleep(60)
            self.connection_callback(False)
        except asyncio.CancelledError:
            return
