from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from asgiref.sync import sync_to_async
from codito_protocol import (
    DeviceReadInput,
    DeviceReadResult,
    FrontendSnapshotResult,
    ProjectApplyPatchResult,
    ProjectCodeResult,
    ProjectManageResult,
    ProjectReadResult,
    ProjectShellResult,
    compute_action_digest,
    validate_project_apply_patch,
    validate_project_code,
    validate_project_frontend,
    validate_project_frontend_result,
    validate_project_manage,
    validate_project_read,
    validate_project_shell,
)
from codito_protocol.desktop_action import DeviceDesktopInput, DeviceDesktopResult
from codito_protocol.screenshot import (
    DeviceScreenshotInput,
    DeviceScreenshotResult,
    ScreenshotToolResult,
    durable_tool_result,
)
from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone
from pydantic import TypeAdapter, ValidationError

from .authz import AuthorizationFailure, MCPPrincipal
from .models import AuditEvent, Device, Operation, Project


class ToolDispatchError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def structured(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


_RELAY_ERROR_CODE_MAP = {
    "authorization_context_missing": "unauthenticated",
    "device_not_found": "device_revoked",
    "device_offline": "device_offline",
    "device_queue_full": "queue_full",
    "idempotency_conflict": "idempotency_conflict",
    "insufficient_scope": "scope_missing",
    "invalid_request": "invalid_request",
    "operation_in_progress": "rate_limited",
    "operation_timeout": "deadline_exceeded",
    "project_not_found": "project_not_found",
    "protocol_error": "internal_error",
    "relay_unavailable": "internal_error",
    "unknown_tool": "invalid_request",
}


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    operation_id: str
    result: dict[str, Any]


class OperationTransport(Protocol):
    async def publish_and_wait(
        self, *, device_id: str, epoch: int, envelope: dict[str, Any], timeout_seconds: int
    ) -> dict[str, Any]: ...


class OfflineTransport:
    async def publish_and_wait(
        self, *, device_id: str, epoch: int, envelope: dict[str, Any], timeout_seconds: int
    ) -> dict[str, Any]:
        raise ToolDispatchError("device_offline", "The Windows device is offline", retryable=True)


class InMemoryTransport:
    """Deterministic transport for contract tests and local development."""

    def __init__(
        self, handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    ) -> None:
        self.handler = handler
        self.envelopes: list[dict[str, Any]] = []

    async def publish_and_wait(
        self, *, device_id: str, epoch: int, envelope: dict[str, Any], timeout_seconds: int
    ) -> dict[str, Any]:
        self.envelopes.append(envelope)
        if self.handler is None:
            raise ToolDispatchError(
                "device_offline", "No in-process device handler is attached", retryable=True
            )
        return await asyncio.wait_for(self.handler(envelope), timeout=timeout_seconds)


class RedisStreamTransport:
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.REDIS_URL

    async def publish_and_wait(
        self, *, device_id: str, epoch: int, envelope: dict[str, Any], timeout_seconds: int
    ) -> dict[str, Any]:
        from redis.asyncio import from_url

        redis = from_url(self.redis_url, decode_responses=True)
        dispatch_stream = f"codito:device:{device_id}:dispatch"
        result_stream = f"codito:operation:{envelope['correlation_id']}:results"
        try:
            await redis.xadd(
                dispatch_stream,
                {
                    "correlation_id": envelope["correlation_id"],
                    "epoch": str(epoch),
                    "envelope": json.dumps(envelope, separators=(",", ":")),
                },
                maxlen=settings.DEVICE_STREAM_MAXLEN,
                approximate=False,
            )
            cursor = "0-0"
            loop = asyncio.get_running_loop()
            expires_at = loop.time() + timeout_seconds
            while True:
                remaining_ms = max(1, int((expires_at - loop.time()) * 1000))
                if remaining_ms <= 1:
                    raise ToolDispatchError(
                        "operation_timeout",
                        "The device did not return a result before the deadline",
                        retryable=True,
                    )
                rows = await redis.xread({result_stream: cursor}, count=8, block=remaining_ms)
                if not rows:
                    raise ToolDispatchError(
                        "operation_timeout",
                        "The device did not return a result before the deadline",
                        retryable=True,
                    )
                for _, messages in rows:
                    for message_id, fields in messages:
                        cursor = message_id
                        payload = fields.get("envelope")
                        if not payload:
                            raise ToolDispatchError(
                                "protocol_error", "Device result omitted its envelope"
                            )
                        try:
                            from codito_protocol import MessageKind, TunnelEnvelope

                            result_envelope = TunnelEnvelope.model_validate_json(payload)
                        except (ImportError, ValueError) as exc:
                            raise ToolDispatchError(
                                "protocol_error", "Device returned an invalid envelope"
                            ) from exc
                        bindings = result_envelope.bindings
                        if (
                            result_envelope.correlation_id != envelope["correlation_id"]
                            or bindings.device_id != device_id
                            or result_envelope.action_digest != envelope["action_digest"]
                            or bindings.account_id != envelope["bindings"]["account_id"]
                            or bindings.link_id != envelope["bindings"].get("link_id")
                            or bindings.grant_id != envelope["bindings"].get("grant_id")
                            or bindings.project_id != envelope["bindings"].get("project_id")
                        ):
                            raise ToolDispatchError(
                                "protocol_error", "Device result binding does not match the request"
                            )
                        if result_envelope.kind is MessageKind.OPERATION_RESULT:
                            current_epoch = await sync_to_async(
                                Operation.objects.values_list("connection_epoch", flat=True).get,
                                thread_sensitive=True,
                            )(correlation_id=envelope["correlation_id"])
                            if result_envelope.connection_epoch != current_epoch:
                                raise ToolDispatchError(
                                    "protocol_error", "Device result used a stale connection epoch"
                                )
                            return result_envelope.payload
        finally:
            await redis.close()


def _canonical_digest(arguments: Mapping[str, Any]) -> str:
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_device_result(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("ok") is False:
        try:
            from codito_protocol import ToolFailure

            return ToolFailure.model_validate(payload).model_dump(mode="json", exclude_none=True)
        except (ImportError, ValidationError) as exc:
            raise ToolDispatchError(
                "protocol_error", "Device returned an invalid typed failure"
            ) from exc
    if payload.get("ok") is not True or not isinstance(payload.get("result"), dict):
        raise ToolDispatchError("protocol_error", "Device result omitted ok/result fields")
    try:
        validated_result: Any
        if tool_name == "device_desktop":
            validated_result = DeviceDesktopResult.model_validate(payload["result"])
        elif tool_name == "device_screenshot":
            validated_result = TypeAdapter(ScreenshotToolResult).validate_python(payload["result"])
        elif tool_name == "device_read":
            validated_result = DeviceReadResult.model_validate(payload["result"])
        elif tool_name == "project_read":
            validated_result = TypeAdapter(ProjectReadResult).validate_python(payload["result"])
        elif tool_name == "project_apply_patch":
            validated_result = TypeAdapter(ProjectApplyPatchResult).validate_python(
                payload["result"]
            )
        elif tool_name == "project_shell":
            validated_result = TypeAdapter(ProjectShellResult).validate_python(payload["result"])
        elif tool_name == "project_manage":
            validated_result = TypeAdapter(ProjectManageResult).validate_python(payload["result"])
        elif tool_name == "project_code":
            validated_result = TypeAdapter(ProjectCodeResult).validate_python(payload["result"])
        elif tool_name == "project_frontend":
            validated_result = validate_project_frontend_result(payload["result"])
        else:
            raise KeyError(tool_name)
    except (ImportError, KeyError, ValidationError) as exc:
        raise ToolDispatchError(
            "protocol_error", "Device result failed its shared tool contract"
        ) from exc
    normalized = dict(payload)
    normalized["result"] = validated_result.model_dump(mode="json", exclude_none=True)
    return normalized


_RESULT_ECHO_FIELDS = (
    "operation",
    "action",
    "project_id",
    "session_id",
    "snapshot_id",
    "element_id",
    "job_id",
    "idempotency_key",
    "path",
    "scope_path",
    "browser",
    "url",
    "display",
    "viewport",
    "dry_run",
    "title",
)


def _result_binding_error() -> ToolDispatchError:
    return ToolDispatchError(
        "protocol_error", "Device result does not match the authorized request"
    )


def _normalized_http_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() != "http" or not parsed.hostname:
        return None
    hostname = parsed.hostname.casefold()
    if hostname.endswith(".."):
        return None
    return ("http", hostname.removesuffix("."), port or 80)


def _validate_device_result_for_request(
    tool_name: str,
    arguments: Mapping[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate result shape and every request identity the result promises to echo."""

    normalized = _validate_device_result(tool_name, payload)
    if normalized.get("ok") is not True:
        return normalized
    result = normalized.get("result")
    if not isinstance(result, dict):
        raise _result_binding_error()
    for field in _RESULT_ECHO_FIELDS:
        if field in arguments and field in result and result[field] != arguments[field]:
            raise _result_binding_error()

    if tool_name == "project_frontend":
        operation = arguments.get("operation")
        if operation == "session_start":
            base_url = result.get("base_url")
            page_url = result.get("url")
            if not isinstance(base_url, str) or not isinstance(page_url, str):
                raise _result_binding_error()
            # Same-origin dev-server redirects (commonly /dashboard -> /login)
            # are legitimate. The shared result model has already rejected
            # credentials, queries, fragments, non-loopback hosts, and HTTPS.
            base_origin = _normalized_http_origin(base_url)
            if base_origin is None or _normalized_http_origin(page_url) != base_origin:
                raise _result_binding_error()
        elif operation == "inspect" and isinstance(arguments.get("element_id"), str):
            element = result.get("element")
            if (
                not isinstance(element, dict)
                or element.get("element_id") != arguments["element_id"]
            ):
                raise _result_binding_error()
    return normalized


def _required_scopes(tool_name: str, operation: str) -> frozenset[str]:
    if tool_name == "device_desktop":
        return frozenset({"shell:execute"})
    if tool_name == "device_screenshot":
        return frozenset({"screen:read"})
    if tool_name == "device_read":
        return frozenset({"files:read"})
    if tool_name == "project_read":
        return (
            frozenset({"projects:read"})
            if operation == "list_projects"
            else frozenset({"projects:read", "files:read"})
        )
    if tool_name == "project_apply_patch":
        return frozenset({"projects:read", "files:read", "files:write"})
    if tool_name == "project_shell":
        return frozenset({"projects:read", "shell:execute"})
    if tool_name == "project_manage":
        return (
            frozenset({"projects:read"})
            if operation == "get_projects"
            else frozenset({"projects:read", "projects:write"})
        )
    if tool_name == "project_code":
        scopes = {"projects:read", "files:read"}
        if operation not in {"code_intelligence_status", "code_workspace_summary"}:
            scopes.add("shell:execute")
        return frozenset(scopes)
    if tool_name == "project_frontend":
        scopes = {"projects:read"}
        if operation == "session_start":
            scopes.update({"frontend:interact", "shell:execute"})
        elif operation in {"snapshot", "inspect"}:
            scopes.add("frontend:read")
        elif operation == "source":
            scopes.update({"frontend:read", "files:read"})
        elif operation in {"act", "session_stop"}:
            scopes.add("frontend:interact")
        return frozenset(scopes)
    raise ToolDispatchError("unknown_tool", f"Unknown tool {tool_name}")


def _validate_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"approved", "trusted", "absolute_path", "project_path", "root_path"}
    supplied = forbidden.intersection(arguments)
    if supplied:
        raise ToolDispatchError(
            "invalid_request",
            "Approval, trust, and absolute paths are controlled locally",
            details={"fields": sorted(supplied)},
        )
    try:
        if tool_name == "device_desktop":
            return DeviceDesktopInput.model_validate(arguments).model_dump(
                mode="json", exclude_none=True
            )
        if tool_name == "device_screenshot":
            return DeviceScreenshotInput.model_validate(arguments).model_dump(
                mode="json", exclude_none=True
            )
        if tool_name == "device_read":
            return DeviceReadInput.model_validate(arguments).model_dump(
                mode="json", exclude_none=True
            )
        if tool_name == "project_read":
            validated = validate_project_read(arguments)
            return validated.model_dump(mode="json", exclude_none=True)
        if tool_name == "project_shell":
            validated_shell = validate_project_shell(arguments)
            return validated_shell.model_dump(mode="json", exclude_none=True)
        if tool_name == "project_apply_patch":
            return validate_project_apply_patch(arguments).model_dump(
                mode="json", exclude_none=True
            )
        if tool_name == "project_manage":
            return validate_project_manage(arguments).model_dump(mode="json", exclude_none=True)
        if tool_name == "project_code":
            return validate_project_code(arguments).model_dump(mode="json", exclude_none=True)
        if tool_name == "project_frontend":
            validated_frontend = validate_project_frontend(arguments)
            return validated_frontend.model_dump(mode="json", exclude_none=True)
    except (ValueError, TypeError) as exc:
        raise ToolDispatchError("invalid_request", str(exc)) from exc
    if tool_name == "project_read" and "operation" not in arguments:
        raise ToolDispatchError("invalid_request", "operation is required")
    if tool_name == "project_shell" and "action" not in arguments:
        raise ToolDispatchError("invalid_request", "action is required")
    if tool_name == "project_code" and "operation" not in arguments:
        raise ToolDispatchError("invalid_request", "operation is required")
    if tool_name == "project_frontend" and "operation" not in arguments:
        raise ToolDispatchError("invalid_request", "operation is required")
    return arguments


def _is_replay_unsafe(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    """Return whether a lost response could conceal an already executed action."""

    return (
        tool_name == "device_desktop"
        or (tool_name == "project_shell" and arguments.get("action") == "start")
        or (
            tool_name == "project_frontend"
            and arguments.get("operation") in {"session_start", "act"}
        )
    )


def _uncertainty_message(tool_name: str, arguments: Mapping[str, Any]) -> str:
    if tool_name == "device_desktop":
        return "The browser action may have executed; do not automatically retry it"
    if tool_name == "project_frontend" and arguments.get("operation") == "session_start":
        return "The frontend session may have started; do not automatically start it again"
    if tool_name == "project_frontend":
        return "The browser interaction may have executed; do not automatically retry it"
    return "The shell command may have started; do not automatically resubmit it"


def _lookup_route(
    principal: MCPPrincipal, tool_name: str, arguments: dict[str, Any]
) -> tuple[Device, Project | None]:
    try:
        device = Device.objects.get(
            pk=principal.device_id, account_id=principal.account_id, revoked_at__isnull=True
        )
    except Device.DoesNotExist as exc:
        raise ToolDispatchError("device_not_found", "The bound device no longer exists") from exc
    operation = str(arguments.get("operation", ""))
    if tool_name in {"device_read", "device_screenshot", "device_desktop"} and not arguments.get(
        "project_id"
    ):
        return device, None
    if (tool_name == "project_read" and operation == "list_projects") or (
        tool_name == "project_manage" and operation in {"get_projects", "request_add_project"}
    ):
        return device, None
    project_id = arguments.get("project_id")
    if not project_id:
        raise ToolDispatchError("invalid_request", "project_id is required")
    try:
        project = Project.objects.get(
            pk=project_id,
            account_id=principal.account_id,
            device_id=principal.device_id,
            status=Project.Status.AVAILABLE,
        )
    except (Project.DoesNotExist, ValueError) as exc:
        raise ToolDispatchError(
            "project_not_found", "Project is not registered on this device"
        ) from exc
    return device, project


def _list_projects(principal: MCPPrincipal, arguments: dict[str, Any]) -> dict[str, Any]:
    projects = Project.objects.filter(
        account_id=principal.account_id,
        device_id=principal.device_id,
        status=Project.Status.AVAILABLE,
    ).order_by("id")
    binding = [principal.account_id, str(principal.device_id), str(principal.link_id)]
    cursor = arguments.get("cursor")
    if cursor:
        try:
            decoded = signing.loads(cursor, salt="codito.projects-list.v1", max_age=3600)
            if (
                not isinstance(decoded, dict)
                or decoded.get("binding") != binding
                or not isinstance(decoded.get("id"), str)
            ):
                raise ValueError("Invalid cursor binding")
        except (signing.BadSignature, ValueError, TypeError) as exc:
            raise ToolDispatchError(
                "invalid_request", "Project cursor is invalid or expired"
            ) from exc
        projects = projects.filter(id__gt=decoded["id"])
    limit = int(arguments.get("limit", 50))
    page = list(projects[: limit + 1])
    device = Device.objects.get(pk=principal.device_id, account_id=principal.account_id)
    online = (
        device.status == Device.Status.ONLINE
        and device.last_seen_at is not None
        and (timezone.now() - device.last_seen_at).total_seconds()
        <= settings.DEVICE_OFFLINE_AFTER_SECONDS
    )
    continuation = None
    if len(page) > limit:
        last = page[limit - 1]
        continuation = {
            "cursor": signing.dumps(
                {"binding": binding, "id": str(last.pk)},
                salt="codito.projects-list.v1",
                compress=True,
            )
        }
    return {
        "operation": "list_projects",
        "projects": [
            {
                "project_id": str(project.pk),
                "title": project.title,
                "device_id": str(project.device_id),
                "mode": project.mode,
                "online": online,
            }
            for project in page[:limit]
        ],
        "continuation": continuation,
    }


def _list_managed_projects(principal: MCPPrincipal, arguments: dict[str, Any]) -> dict[str, Any]:
    result = _list_projects(principal, arguments)
    result["operation"] = "get_projects"
    return result


def _create_operation(
    *,
    principal: MCPPrincipal,
    device: Device,
    project: Project | None,
    tool_name: str,
    arguments: dict[str, Any],
    action_digest: str,
    timeout_seconds: int,
) -> tuple[Operation, bool]:
    idempotency_key = str(arguments.get("idempotency_key", ""))
    idempotency_scope = {
        "account_id": principal.account_id,
        "oauth_grant_id": principal.oauth_grant_id,
        "device_link_id": principal.device_link_id,
        "device": device,
        "project": project,
        "kind": tool_name,
        "idempotency_key": idempotency_key,
    }
    if idempotency_key:
        existing = Operation.objects.filter(**idempotency_scope).first()
        if existing:
            if existing.action_digest != action_digest:
                raise ToolDispatchError(
                    "idempotency_conflict", "Idempotency key was already used for another action"
                )
            return existing, False
    pending_count = Operation.objects.filter(
        device=device,
        status__in=[
            Operation.Status.ACCEPTED,
            Operation.Status.DISPATCHED,
            Operation.Status.RECEIVED,
            Operation.Status.RUNNING,
        ],
    ).count()
    if pending_count >= settings.DEVICE_QUEUE_LIMIT:
        raise ToolDispatchError(
            "device_queue_full", "The device pending queue is full", retryable=True
        )
    deadline = timezone.now() + timedelta(seconds=timeout_seconds)
    try:
        with transaction.atomic():
            operation = Operation.objects.create(
                account_id=principal.account_id,
                oauth_grant_id=principal.oauth_grant_id,
                device=device,
                device_link_id=principal.device_link_id,
                project=project,
                kind=tool_name,
                connection_epoch=device.connection_epoch,
                idempotency_key=idempotency_key,
                action_digest=action_digest,
                request_digest=_canonical_digest(arguments),
                request_payload={"tool_name": tool_name, "input": arguments},
                deadline_at=deadline,
            )
            AuditEvent.objects.create(
                account_id=principal.account_id,
                actor_type="oauth_grant",
                actor_id=principal.oauth_grant_id,
                event_type="operation.accepted",
                device=device,
                project=project,
                operation=operation,
                metadata={"kind": tool_name, "action_digest": action_digest},
            )
            return operation, True
    except IntegrityError:
        if not idempotency_key:
            raise
        existing = Operation.objects.get(**idempotency_scope)
        if existing.action_digest != action_digest:
            raise ToolDispatchError(
                "idempotency_conflict", "Idempotency key was already used for another action"
            ) from None
        return existing, False


def _mark_dispatched(operation_id: uuid.UUID) -> None:
    Operation.objects.filter(pk=operation_id, status=Operation.Status.ACCEPTED).update(
        status=Operation.Status.DISPATCHED
    )


def _finish(operation_id: uuid.UUID, result: dict[str, Any]) -> None:
    terminal_status = (
        Operation.Status.SUCCEEDED if not result.get("error") else Operation.Status.FAILED
    )
    Operation.objects.filter(
        pk=operation_id,
        status__in=[
            Operation.Status.ACCEPTED,
            Operation.Status.DISPATCHED,
            Operation.Status.RECEIVED,
            Operation.Status.RUNNING,
        ],
    ).update(status=terminal_status, result=durable_tool_result(result))


def _mark_delivery_failure(
    operation_id: uuid.UUID, tool_name: str, error: ToolDispatchError
) -> None:
    if error.code == "device_offline":
        status = Operation.Status.FAILED
    elif error.code == "outcome_unknown":
        status = Operation.Status.OUTCOME_UNKNOWN
    else:
        status = Operation.Status.FAILED
    Operation.objects.filter(
        pk=operation_id,
        status__in=[
            Operation.Status.ACCEPTED,
            Operation.Status.DISPATCHED,
            Operation.Status.RECEIVED,
            Operation.Status.RUNNING,
        ],
    ).update(status=status, error_code=error.code)


async def dispatch_tool(
    principal: MCPPrincipal,
    tool_name: str,
    raw_arguments: dict[str, Any],
    *,
    transport: OperationTransport | None = None,
) -> DispatchReceipt:
    arguments = _validate_arguments(tool_name, raw_arguments)
    operation_name = str(arguments.get("operation", ""))
    required_scopes = _required_scopes(tool_name, operation_name)
    if arguments.get("project_id"):
        required_scopes |= frozenset({"projects:read"})
    try:
        for required_scope in required_scopes:
            principal.require(required_scope)
    except AuthorizationFailure as exc:
        raise ToolDispatchError(
            exc.code, exc.description, details={"required_scopes": sorted(required_scopes)}
        ) from exc
    device, project = await sync_to_async(_lookup_route, thread_sensitive=True)(
        principal, tool_name, arguments
    )
    if tool_name == "project_read" and operation_name == "list_projects":
        result = await sync_to_async(_list_projects, thread_sensitive=True)(principal, arguments)
        return DispatchReceipt(operation_id="local", result=result)
    if tool_name == "project_manage" and operation_name == "get_projects":
        result = await sync_to_async(_list_managed_projects, thread_sensitive=True)(
            principal, arguments
        )
        return DispatchReceipt(operation_id="local", result=result)
    now = timezone.now()
    if (
        device.status != Device.Status.ONLINE
        or device.last_seen_at is None
        or (now - device.last_seen_at).total_seconds() > settings.DEVICE_OFFLINE_AFTER_SECONDS
    ):
        last_seen = device.last_seen_at.isoformat() if device.last_seen_at else None
        raise ToolDispatchError(
            "device_offline",
            "The Windows device is offline",
            retryable=True,
            details={"last_seen_at": last_seen},
        )
    wire_payload = {"tool_name": tool_name, "input": arguments}
    digest = compute_action_digest(wire_payload)
    timeout_seconds = settings.OPERATION_TIMEOUT_SECONDS
    if tool_name == "project_frontend" and operation_name == "session_start":
        # The configured start budget covers approval/browser overhead. The
        # caller's separately bounded readiness wait is additive so a slow but
        # permitted dev server cannot consume the entire approval lifecycle.
        ready_timeout = int(arguments.get("ready_timeout_seconds", 30))
        frontend_timeout = min(
            max(settings.FRONTEND_START_TIMEOUT_SECONDS + ready_timeout, 60), 420
        )
        timeout_seconds = max(timeout_seconds, frontend_timeout)
    operation, created = await sync_to_async(_create_operation, thread_sensitive=True)(
        principal=principal,
        device=device,
        project=project,
        tool_name=tool_name,
        arguments=arguments,
        action_digest=digest,
        timeout_seconds=timeout_seconds,
    )
    if not created:
        if operation.result is not None:
            return DispatchReceipt(str(operation.pk), operation.result)
        if operation.status in {
            Operation.Status.SUCCEEDED,
            Operation.Status.FAILED,
            Operation.Status.CANCELLED,
            Operation.Status.EXPIRED,
            Operation.Status.OUTCOME_UNKNOWN,
        }:
            terminal_codes: dict[str, str] = {
                Operation.Status.CANCELLED: "cancelled",
                Operation.Status.EXPIRED: "operation_timeout",
                Operation.Status.OUTCOME_UNKNOWN: "outcome_unknown",
            }
            terminal_code = terminal_codes.get(
                operation.status, operation.error_code or "protocol_error"
            )
            raise ToolDispatchError(
                terminal_code,
                "The prior operation with this idempotency key ended without a result",
                retryable=terminal_code in {"device_offline", "operation_timeout"},
            )
        raise ToolDispatchError(
            "operation_in_progress",
            "An operation with this idempotency key is already in progress",
            retryable=True,
        )
    try:
        from codito_protocol import MessageKind, TunnelBindings, TunnelEnvelope

        envelope = TunnelEnvelope(
            kind=MessageKind.OPERATION,
            message_id=str(operation.message_id),
            correlation_id=str(operation.correlation_id),
            sequence=0,
            connection_epoch=device.connection_epoch,
            deadline_at=operation.deadline_at,
            bindings=TunnelBindings(
                account_id=f"account_{principal.account_id:016x}",
                device_id=str(principal.device_id),
                link_id=str(principal.link_id),
                grant_id=principal.oauth_grant_id,
                project_id=str(project.pk) if project else None,
            ),
            action_digest=digest,
            payload=wire_payload,
        ).model_dump(mode="json", exclude_none=True)
    except (ImportError, ValueError) as exc:
        await sync_to_async(_mark_delivery_failure, thread_sensitive=True)(
            operation.pk, tool_name, ToolDispatchError("protocol_error", str(exc))
        )
        raise ToolDispatchError(
            "protocol_error", "Could not construct a valid tunnel envelope"
        ) from exc
    selected_transport = transport or RedisStreamTransport()
    await sync_to_async(_mark_dispatched, thread_sensitive=True)(operation.pk)
    uncertain_start = _is_replay_unsafe(tool_name, arguments)
    uncertainty_message = _uncertainty_message(tool_name, arguments)
    try:
        result = await selected_transport.publish_and_wait(
            device_id=str(device.pk),
            epoch=device.connection_epoch,
            envelope=envelope,
            timeout_seconds=max(
                1,
                int(
                    min(
                        float(timeout_seconds),
                        (operation.deadline_at - timezone.now()).total_seconds(),
                    )
                ),
            ),
        )
    except ToolDispatchError as exc:
        if uncertain_start and exc.code != "device_offline":
            mapped = ToolDispatchError(
                "outcome_unknown",
                uncertainty_message,
                retryable=False,
                details={"operation_id": str(operation.pk)},
            )
            await sync_to_async(_mark_delivery_failure, thread_sensitive=True)(
                operation.pk, tool_name, mapped
            )
            raise mapped from exc
        await sync_to_async(_mark_delivery_failure, thread_sensitive=True)(
            operation.pk, tool_name, exc
        )
        raise
    except Exception as exc:
        mapped = ToolDispatchError(
            "outcome_unknown" if uncertain_start else "relay_unavailable",
            (
                uncertainty_message
                if uncertain_start
                else "The relay transport is temporarily unavailable"
            ),
            retryable=not uncertain_start,
            details={"operation_id": str(operation.pk)},
        )
        await sync_to_async(_mark_delivery_failure, thread_sensitive=True)(
            operation.pk, tool_name, mapped
        )
        raise mapped from exc
    try:
        validated_result = _validate_device_result_for_request(tool_name, arguments, result)
    except ToolDispatchError as exc:
        if uncertain_start:
            mapped = ToolDispatchError(
                "outcome_unknown",
                uncertainty_message,
                retryable=False,
                details={"operation_id": str(operation.pk)},
            )
            await sync_to_async(_mark_delivery_failure, thread_sensitive=True)(
                operation.pk, tool_name, mapped
            )
            raise mapped from exc
        await sync_to_async(_mark_delivery_failure, thread_sensitive=True)(
            operation.pk, tool_name, exc
        )
        raise
    await sync_to_async(_finish, thread_sensitive=True)(operation.pk, validated_result)
    return DispatchReceipt(str(operation.pk), validated_result)


def success_tool_result(receipt: DispatchReceipt) -> dict[str, Any]:
    failed = receipt.result.get("ok") is False
    text = str(receipt.result.get("text") or f"Codito operation {receipt.operation_id} completed.")
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    structured = {"operation_id": receipt.operation_id, **receipt.result}
    result = receipt.result.get("result")
    if not failed and isinstance(result, dict) and "image_base64" in result:
        image_result = (
            FrontendSnapshotResult.model_validate(result)
            if result.get("operation") == "snapshot"
            else DeviceScreenshotResult.model_validate(result)
        )
        content.append(
            {
                "type": "image",
                "data": image_result.image_base64,
                "mimeType": image_result.mime_type,
            }
        )
        structured["result"] = image_result.model_dump(mode="json", exclude={"image_base64"})
    return {
        "content": content,
        "structuredContent": structured,
        "isError": failed,
    }


def error_tool_result(error: ToolDispatchError) -> dict[str, Any]:
    from codito_protocol import ErrorCode, ToolError, ToolFailure

    try:
        public_code = ErrorCode(error.code)
    except ValueError:
        public_code = ErrorCode(_RELAY_ERROR_CODE_MAP.get(error.code, "internal_error"))
    failure = ToolFailure(
        ok=False,
        text=error.message,
        error=ToolError(
            code=public_code,
            message=error.message,
            retryable=error.retryable,
            details=error.details,
        ),
    ).model_dump(mode="json", exclude_none=True)
    return {
        "content": [{"type": "text", "text": error.message}],
        "structuredContent": failure,
        "isError": True,
    }
