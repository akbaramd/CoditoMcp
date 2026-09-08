from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass
from typing import Any

from asgiref.sync import sync_to_async
from codito_protocol import (
    ErrorCode,
    MessageKind,
    ToolError,
    ToolFailure,
    TunnelBindings,
    TunnelEnvelope,
)
from codito_protocol.screenshot import durable_tool_result
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError
from redis.asyncio import from_url
from starlette.websockets import WebSocket, WebSocketDisconnect

from .device_crypto import verify_device_signature, websocket_proof_message
from .models import AuditEvent, Device, DeviceConnectionTicket, Operation, Project, hash_secret


class DeviceAuthenticationError(Exception):
    pass


_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")


def _new_message_id() -> str:
    return f"relay_{secrets.token_urlsafe(24)}"


@dataclass(frozen=True, slots=True)
class DeviceConnection:
    device_id: str
    account_wire_id: str
    epoch: int


@dataclass(frozen=True, slots=True)
class PreparedDispatch:
    operation_id: str
    outbound: TunnelEnvelope | None = None
    terminal: TunnelEnvelope | None = None


@transaction.atomic
def _consume_ticket(raw_ticket: str, signature: str) -> DeviceConnection:
    ticket = (
        DeviceConnectionTicket.objects.select_for_update()
        .select_related("device")
        .filter(token_hash=hash_secret(raw_ticket))
        .first()
    )
    if ticket is None or not ticket.usable:
        raise DeviceAuthenticationError("ticket is unknown, used, expired, or revoked")
    if not verify_device_signature(
        ticket.device.public_key_jwk,
        websocket_proof_message(raw_ticket, ticket.challenge),
        signature,
    ):
        raise DeviceAuthenticationError("device proof is invalid")
    ticket.used_at = timezone.now()
    ticket.save(update_fields=["used_at"])
    device = Device.objects.select_for_update().get(pk=ticket.device_id)
    device.connection_epoch += 1
    device.status = Device.Status.OFFLINE
    device.save(update_fields=["connection_epoch", "status"])
    return DeviceConnection(
        device_id=str(device.pk),
        account_wire_id=f"account_{device.account_id:016x}",
        epoch=device.connection_epoch,
    )


def _mark_online(connection: DeviceConnection) -> None:
    Device.objects.filter(
        pk=connection.device_id, connection_epoch=connection.epoch, revoked_at__isnull=True
    ).update(status=Device.Status.ONLINE, last_seen_at=timezone.now())


def _touch(connection: DeviceConnection) -> None:
    Device.objects.filter(
        pk=connection.device_id, connection_epoch=connection.epoch, revoked_at__isnull=True
    ).update(last_seen_at=timezone.now())


def _mark_offline(connection: DeviceConnection) -> None:
    Device.objects.filter(pk=connection.device_id, connection_epoch=connection.epoch).exclude(
        status=Device.Status.REVOKED
    ).update(status=Device.Status.OFFLINE)


def _validate_operation_binding(
    connection: DeviceConnection, envelope: TunnelEnvelope
) -> Operation:
    if envelope.bindings.account_id != connection.account_wire_id:
        raise ValueError("account substitution")
    if (
        envelope.bindings.device_id != connection.device_id
        or envelope.connection_epoch != connection.epoch
    ):
        raise ValueError("device or epoch substitution")
    if not envelope.correlation_id:
        raise ValueError("correlation_id is required")
    operation = (
        Operation.objects.select_for_update(of=("self",))
        .select_related("project", "device_link")
        .get(
            correlation_id=envelope.correlation_id,
            account_id=int(connection.account_wire_id.removeprefix("account_"), 16),
            device_id=connection.device_id,
            device__connection_epoch=connection.epoch,
        )
    )
    expected_project = str(operation.project_id) if operation.project_id else None
    if envelope.bindings.project_id != expected_project:
        raise ValueError("project substitution")
    if envelope.bindings.grant_id != operation.oauth_grant_id:
        raise ValueError("grant substitution")
    if envelope.bindings.link_id != str(operation.device_link.link_id):
        raise ValueError("link substitution")
    if envelope.action_digest != operation.action_digest:
        raise ValueError("action digest substitution")
    return operation


@transaction.atomic
def _record_device_message(connection: DeviceConnection, envelope: TunnelEnvelope) -> Operation:
    operation = _validate_operation_binding(connection, envelope)
    states = {
        MessageKind.OPERATION_RECEIVED: Operation.Status.RECEIVED,
        MessageKind.OPERATION_STARTED: Operation.Status.RUNNING,
        MessageKind.OPERATION_RESULT: (
            Operation.Status.FAILED
            if envelope.payload.get("ok") is False or envelope.payload.get("error")
            else Operation.Status.SUCCEEDED
        ),
        MessageKind.TERMINAL_ACK: operation.status,
    }
    status = states.get(envelope.kind)
    terminal_statuses = {
        Operation.Status.SUCCEEDED,
        Operation.Status.FAILED,
        Operation.Status.CANCELLED,
        Operation.Status.EXPIRED,
        Operation.Status.OUTCOME_UNKNOWN,
    }
    if operation.status in terminal_statuses:
        if envelope.kind is MessageKind.OPERATION_RESULT:
            if operation.result == durable_tool_result(envelope.payload):
                return operation
            reconcilable_errors = {
                "device_offline",
                "operation_timeout",
                "outcome_unknown",
                "relay_unavailable",
            }
            if operation.result is None and operation.error_code in reconcilable_errors:
                from .dispatch import ToolDispatchError, _validate_device_result

                try:
                    validated_payload = _validate_device_result(operation.kind, envelope.payload)
                except ToolDispatchError as exc:
                    raise ValueError("late terminal failed its tool result contract") from exc
                prior_status = operation.status
                prior_error = operation.error_code
                operation.status = (
                    Operation.Status.FAILED
                    if validated_payload.get("ok") is False or validated_payload.get("error")
                    else Operation.Status.SUCCEEDED
                )
                operation.result = durable_tool_result(validated_payload)
                device_error = validated_payload.get("error")
                operation.error_code = (
                    str(device_error.get("code", "device_error"))
                    if isinstance(device_error, dict)
                    else ""
                )
                operation.connection_epoch = connection.epoch
                operation.last_device_sequence = envelope.sequence
                operation.last_device_sequence_epoch = connection.epoch
                operation.save(
                    update_fields=[
                        "status",
                        "result",
                        "error_code",
                        "connection_epoch",
                        "last_device_sequence",
                        "last_device_sequence_epoch",
                        "updated_at",
                    ]
                )
                AuditEvent.objects.create(
                    account_id=operation.account_id,
                    actor_type="device",
                    actor_id=connection.device_id,
                    event_type="operation.late_terminal_reconciled",
                    device_id=operation.device_id,
                    project_id=operation.project_id,
                    operation=operation,
                    metadata={
                        "prior_status": prior_status,
                        "prior_error_code": prior_error,
                        "resolved_status": operation.status,
                    },
                )
                return operation
            raise ValueError("terminal result substitution")
        return operation

    if (
        operation.last_device_sequence_epoch == connection.epoch
        and envelope.sequence <= operation.last_device_sequence
    ):
        return operation

    state_rank: dict[str, int] = {
        Operation.Status.ACCEPTED: 0,
        Operation.Status.DISPATCHED: 1,
        Operation.Status.RECEIVED: 2,
        Operation.Status.RUNNING: 3,
        Operation.Status.SUCCEEDED: 4,
        Operation.Status.FAILED: 4,
    }
    if status and state_rank.get(status, 0) >= state_rank.get(operation.status, 0):
        operation.status = status
    if envelope.kind is MessageKind.OPERATION_RESULT:
        operation.result = durable_tool_result(envelope.payload)
    operation.connection_epoch = connection.epoch
    operation.last_device_sequence = envelope.sequence
    operation.last_device_sequence_epoch = connection.epoch
    operation.save(
        update_fields=[
            "status",
            "result",
            "connection_epoch",
            "last_device_sequence",
            "last_device_sequence_epoch",
            "updated_at",
        ]
    )
    return operation


def _failure_payload(
    code: ErrorCode,
    message: str,
    correlation_id: str,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    return ToolFailure(
        ok=False,
        text=message,
        error=ToolError(
            code=code,
            message=message,
            retryable=retryable,
            correlation_id=correlation_id,
        ),
    ).model_dump(mode="json", exclude_none=True)


def _terminal_result_envelope(
    connection: DeviceConnection, operation: Operation, payload: dict[str, Any]
) -> TunnelEnvelope:
    return TunnelEnvelope(
        kind=MessageKind.OPERATION_RESULT,
        message_id=_new_message_id(),
        correlation_id=str(operation.correlation_id),
        sequence=0,
        connection_epoch=connection.epoch,
        bindings=TunnelBindings(
            account_id=connection.account_wire_id,
            device_id=connection.device_id,
            link_id=str(operation.device_link.link_id),
            grant_id=operation.oauth_grant_id,
            project_id=str(operation.project_id) if operation.project_id else None,
        ),
        action_digest=operation.action_digest,
        payload=payload,
    )


@transaction.atomic
def _prepare_operation_dispatch(
    connection: DeviceConnection, correlation_id: str
) -> PreparedDispatch | None:
    account_id = int(connection.account_wire_id.removeprefix("account_"), 16)
    operation = (
        Operation.objects.select_for_update(of=("self",))
        .select_related("device", "device_link", "project")
        .filter(
            correlation_id=correlation_id,
            account_id=account_id,
            device_id=connection.device_id,
        )
        .first()
    )
    if operation is None:
        return None
    if operation.device.connection_epoch != connection.epoch:
        raise ValueError("connection was fenced while dispatching")
    if operation.status in {
        Operation.Status.SUCCEEDED,
        Operation.Status.FAILED,
        Operation.Status.CANCELLED,
        Operation.Status.EXPIRED,
        Operation.Status.OUTCOME_UNKNOWN,
    }:
        return None
    now = timezone.now()
    terminal_payload: dict[str, Any] | None = None
    terminal_status: str | None = None
    terminal_error = ""
    if operation.deadline_at <= now:
        terminal_status = Operation.Status.EXPIRED
        terminal_error = "operation_timeout"
        terminal_payload = _failure_payload(
            ErrorCode.DEADLINE_EXCEEDED,
            "The operation deadline elapsed before device delivery",
            str(operation.correlation_id),
            retryable=True,
        )
    elif operation.device_link.revoked_at is not None:
        terminal_status = Operation.Status.FAILED
        terminal_error = "link_revoked"
        terminal_payload = _failure_payload(
            ErrorCode.LINK_REVOKED,
            "The OAuth device link was revoked before delivery",
            str(operation.correlation_id),
        )
    elif operation.delivery_attempted_at is not None and operation.kind == Operation.Kind.SHELL:
        terminal_status = Operation.Status.OUTCOME_UNKNOWN
        terminal_error = "outcome_unknown"
        terminal_payload = _failure_payload(
            ErrorCode.OUTCOME_UNKNOWN,
            "The relay disconnected after shell delivery became uncertain; it was not replayed",
            str(operation.correlation_id),
        )
    if terminal_payload is not None and terminal_status is not None:
        operation.status = terminal_status
        operation.error_code = terminal_error
        # Relay-derived timeout/uncertainty is not an authoritative device
        # result. Keep the durable result slot empty so a later journaled
        # terminal from the bound device can reconcile it exactly once.
        operation.result = (
            None if terminal_error in {"operation_timeout", "outcome_unknown"} else terminal_payload
        )
        operation.connection_epoch = connection.epoch
        operation.save(
            update_fields=[
                "status",
                "error_code",
                "result",
                "connection_epoch",
                "updated_at",
            ]
        )
        return PreparedDispatch(
            operation_id=str(operation.pk),
            terminal=_terminal_result_envelope(connection, operation, terminal_payload),
        )

    operation.connection_epoch = connection.epoch
    operation.delivery_attempted_at = now
    operation.sent_at = None
    operation.sent_epoch = None
    operation.save(
        update_fields=[
            "connection_epoch",
            "delivery_attempted_at",
            "sent_at",
            "sent_epoch",
            "updated_at",
        ]
    )
    envelope = TunnelEnvelope(
        kind=MessageKind.OPERATION,
        message_id=str(operation.message_id),
        correlation_id=str(operation.correlation_id),
        sequence=0,
        connection_epoch=connection.epoch,
        deadline_at=operation.deadline_at,
        bindings=TunnelBindings(
            account_id=connection.account_wire_id,
            device_id=connection.device_id,
            link_id=str(operation.device_link.link_id),
            grant_id=operation.oauth_grant_id,
            project_id=str(operation.project_id) if operation.project_id else None,
        ),
        action_digest=operation.action_digest,
        payload=operation.request_payload,
    )
    return PreparedDispatch(operation_id=str(operation.pk), outbound=envelope)


def _mark_operation_sent(operation_id: str, connection: DeviceConnection) -> None:
    Operation.objects.filter(
        pk=operation_id,
        device_id=connection.device_id,
        connection_epoch=connection.epoch,
        sent_at__isnull=True,
    ).update(sent_at=timezone.now(), sent_epoch=connection.epoch)


def _recoverable_operation_correlations(connection: DeviceConnection) -> list[str]:
    return [
        str(value)
        for value in Operation.objects.filter(
            account_id=int(connection.account_wire_id.removeprefix("account_"), 16),
            device_id=connection.device_id,
            status__in=[
                Operation.Status.DISPATCHED,
                Operation.Status.RECEIVED,
                Operation.Status.RUNNING,
            ],
        )
        .order_by("created_at")
        .values_list("correlation_id", flat=True)[:100]
    ]


async def _deliver_operation(
    websocket: WebSocket, redis: Any, connection: DeviceConnection, correlation_id: str
) -> None:
    prepared = await sync_to_async(_prepare_operation_dispatch, thread_sensitive=True)(
        connection, correlation_id
    )
    if prepared is None:
        return
    if prepared.terminal is not None:
        stream = f"codito:operation:{correlation_id}:results"
        await redis.xadd(
            stream,
            {"envelope": prepared.terminal.model_dump_json(exclude_none=True)},
            maxlen=256,
            approximate=True,
        )
        await redis.expire(stream, 300)
        return
    if prepared.outbound is None:
        return
    await websocket.send_json(prepared.outbound.model_dump(mode="json", exclude_none=True))
    await sync_to_async(_mark_operation_sent, thread_sensitive=True)(
        prepared.operation_id, connection
    )


async def _recover_unsent_operations(
    websocket: WebSocket, redis: Any, connection: DeviceConnection
) -> None:
    correlations = await sync_to_async(_recoverable_operation_correlations, thread_sensitive=True)(
        connection
    )
    for correlation_id in correlations:
        await _deliver_operation(websocket, redis, connection, correlation_id)


def _terminal_ack(
    connection: DeviceConnection, operation: Operation, envelope: TunnelEnvelope
) -> TunnelEnvelope:
    return TunnelEnvelope(
        kind=MessageKind.TERMINAL_ACK,
        message_id=_new_message_id(),
        correlation_id=envelope.correlation_id,
        sequence=envelope.sequence + 1,
        connection_epoch=connection.epoch,
        bindings=envelope.bindings,
        action_digest=operation.action_digest,
        payload={"persisted": True},
    )


def _synchronize_projects(connection: DeviceConnection, payload: dict[str, Any]) -> None:
    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list) or len(raw_projects) > 1000:
        raise ValueError("invalid project synchronization payload")
    account_id = int(connection.account_wire_id.removeprefix("account_"), 16)
    seen: set[str] = set()
    with transaction.atomic():
        for item in raw_projects:
            if not isinstance(item, dict):
                raise ValueError("invalid project entry")
            project_id = str(item.get("project_id", ""))
            title = str(item.get("title", ""))
            fingerprint = str(item.get("root_fingerprint", ""))
            mode = str(item.get("mode", "isolated"))
            if not (
                _OPAQUE_ID_RE.fullmatch(project_id)
                and 1 <= len(title) <= 160
                and 32 <= len(fingerprint) <= 128
            ):
                raise ValueError("invalid project identifiers")
            if mode not in Project.Mode.values:
                raise ValueError("invalid project mode")
            project = Project.objects.select_for_update().filter(pk=project_id).first()
            if project is not None and (
                project.account_id != account_id or str(project.device_id) != connection.device_id
            ):
                raise ValueError("cross-account or cross-device project substitution")
            if project is None:
                Project.objects.create(
                    id=project_id,
                    account_id=account_id,
                    device_id=connection.device_id,
                    title=title,
                    root_fingerprint=fingerprint,
                    mode=mode,
                    status=Project.Status.AVAILABLE,
                )
            else:
                project.title = title
                project.root_fingerprint = fingerprint
                project.mode = mode
                project.status = Project.Status.AVAILABLE
                project.save(
                    update_fields=["title", "root_fingerprint", "mode", "status", "updated_at"]
                )
            seen.add(project_id)
        Project.objects.filter(account_id=account_id, device_id=connection.device_id).exclude(
            pk__in=seen
        ).update(status=Project.Status.UNAVAILABLE)


async def _receive_loop(websocket: WebSocket, redis: Any, connection: DeviceConnection) -> None:
    while True:
        try:
            data = await asyncio.wait_for(websocket.receive_json(), timeout=75)
        except TimeoutError:
            await websocket.close(code=1001, reason="device heartbeat timed out")
            return
        try:
            envelope = TunnelEnvelope.model_validate(data)
            if envelope.connection_epoch != connection.epoch:
                raise ValueError("stale connection epoch")
            if envelope.bindings.account_id != connection.account_wire_id:
                raise ValueError("account substitution")
            if envelope.bindings.device_id != connection.device_id:
                raise ValueError("device substitution")
            if envelope.kind is MessageKind.HELLO and "projects" in envelope.payload:
                await sync_to_async(_synchronize_projects, thread_sensitive=True)(
                    connection, envelope.payload
                )
            elif envelope.kind in {
                MessageKind.OPERATION_RECEIVED,
                MessageKind.OPERATION_STARTED,
                MessageKind.OPERATION_PROGRESS,
                MessageKind.OPERATION_RESULT,
                MessageKind.TERMINAL_ACK,
            }:
                operation = await sync_to_async(_record_device_message, thread_sensitive=True)(
                    connection, envelope
                )
                if envelope.kind in {MessageKind.OPERATION_PROGRESS, MessageKind.OPERATION_RESULT}:
                    stream = f"codito:operation:{envelope.correlation_id}:results"
                    await redis.xadd(
                        stream,
                        {"envelope": envelope.model_dump_json(exclude_none=True)},
                        maxlen=256,
                        approximate=True,
                    )
                    await redis.expire(stream, 300)
                if envelope.kind is MessageKind.OPERATION_RESULT:
                    acknowledgement = _terminal_ack(connection, operation, envelope)
                    await websocket.send_json(
                        acknowledgement.model_dump(mode="json", exclude_none=True)
                    )
            elif envelope.kind is MessageKind.HEARTBEAT:
                reply = TunnelEnvelope(
                    kind=MessageKind.HEARTBEAT_ACK,
                    message_id=_new_message_id(),
                    correlation_id=envelope.message_id,
                    sequence=envelope.sequence,
                    connection_epoch=connection.epoch,
                    bindings=TunnelBindings(
                        account_id=connection.account_wire_id, device_id=connection.device_id
                    ),
                )
                await websocket.send_json(reply.model_dump(mode="json", exclude_none=True))
            await sync_to_async(_touch, thread_sensitive=True)(connection)
        except (ValidationError, ValueError, Operation.DoesNotExist):
            await websocket.close(code=1008, reason="invalid or substituted envelope")
            return


async def _dispatch_loop(
    websocket: WebSocket, redis: Any, connection: DeviceConnection, cursor: str
) -> None:
    stream = f"codito:device:{connection.device_id}:dispatch"
    fence_key = f"codito:device:{connection.device_id}:epoch"
    await _recover_unsent_operations(websocket, redis, connection)
    while True:
        current_epoch = await redis.get(fence_key)
        if current_epoch is None or int(current_epoch) != connection.epoch:
            await websocket.close(code=1008, reason="connection fenced")
            return
        await redis.expire(fence_key, 120)
        rows = await redis.xread({stream: cursor}, count=16, block=1000)
        if not rows:
            continue
        for _, messages in rows:
            for message_id, fields in messages:
                cursor = message_id
                correlation_id = fields.get("correlation_id")
                if not correlation_id:
                    queued = TunnelEnvelope.model_validate_json(fields["envelope"])
                    correlation_id = queued.correlation_id
                if correlation_id:
                    await _deliver_operation(websocket, redis, connection, correlation_id)


async def _heartbeat_loop(websocket: WebSocket, connection: DeviceConnection) -> None:
    sequence = 0
    while True:
        await asyncio.sleep(30)
        sequence += 1
        envelope = TunnelEnvelope(
            kind=MessageKind.HEARTBEAT,
            message_id=f"heartbeat_{connection.epoch:08x}_{sequence:08x}",
            sequence=sequence,
            connection_epoch=connection.epoch,
            bindings=TunnelBindings(
                account_id=connection.account_wire_id, device_id=connection.device_id
            ),
        )
        await websocket.send_json(envelope.model_dump(mode="json", exclude_none=True))


async def device_websocket(websocket: WebSocket) -> None:
    from django.conf import settings

    authorization = websocket.headers.get("authorization", "")
    scheme, separator, raw_ticket = authorization.partition(" ")
    signature = websocket.headers.get("x-codito-device-proof", "")
    offered_protocols = {
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    }
    if not separator or scheme != "DeviceTicket" or "codito.device.v1" not in offered_protocols:
        await websocket.close(code=1008, reason="device ticket or protocol missing")
        return
    try:
        connection = await sync_to_async(_consume_ticket, thread_sensitive=True)(
            raw_ticket, signature
        )
    except DeviceAuthenticationError:
        await websocket.close(code=1008, reason="device authentication failed")
        return
    redis = from_url(settings.REDIS_URL, decode_responses=True)
    try:
        cursor = "0-0"
        await redis.set(
            f"codito:device:{connection.device_id}:epoch", str(connection.epoch), ex=120
        )
        await websocket.accept(subprotocol="codito.device.v1")
        welcome = TunnelEnvelope(
            kind=MessageKind.WELCOME,
            message_id=f"welcome_{connection.epoch:08x}_00000000",
            sequence=0,
            connection_epoch=connection.epoch,
            bindings=TunnelBindings(
                account_id=connection.account_wire_id,
                device_id=connection.device_id,
            ),
            payload={
                "heartbeat_interval_seconds": 30,
                "offline_after_seconds": 75,
                "queue_limit": settings.DEVICE_QUEUE_LIMIT,
            },
        )
        await websocket.send_json(welcome.model_dump(mode="json", exclude_none=True))
        await sync_to_async(_mark_online, thread_sensitive=True)(connection)
        tasks = {
            asyncio.create_task(_receive_loop(websocket, redis, connection)),
            asyncio.create_task(_dispatch_loop(websocket, redis, connection, cursor)),
            asyncio.create_task(_heartbeat_loop(websocket, connection)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    except (WebSocketDisconnect, RuntimeError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        await sync_to_async(_mark_offline, thread_sensitive=True)(connection)
        await redis.close()
