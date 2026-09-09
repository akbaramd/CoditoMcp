"""Versioned, fenced device tunnel envelope."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .code import ProjectCodeInput
from .desktop_action import DeviceDesktopInput
from .device_read import DeviceReadInput
from .digest import compute_action_digest
from .manage import ProjectManageInput
from .patch import ProjectApplyPatchInput
from .read import ProjectReadInput
from .screenshot import DeviceScreenshotInput
from .shell import ProjectShellInput
from .types import PROTOCOL_VERSION, CoditoModel, OpaqueId, Sha256, ensure_utc, utc_now


class MessageKind(StrEnum):
    HELLO = "hello"
    WELCOME = "welcome"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    OPERATION = "operation"
    OPERATION_RECEIVED = "operation_received"
    OPERATION_STARTED = "operation_started"
    OPERATION_PROGRESS = "operation_progress"
    OPERATION_RESULT = "operation_result"
    OPERATION_CANCEL = "operation_cancel"
    TERMINAL_ACK = "terminal_ack"
    ERROR = "error"
    GOODBYE = "goodbye"


class TunnelBindings(CoditoModel):
    account_id: OpaqueId
    device_id: OpaqueId
    link_id: OpaqueId | None = None
    grant_id: OpaqueId | None = None
    project_id: OpaqueId | None = None


class OperationPayload(CoditoModel):
    tool_name: Literal[
        "project_read",
        "project_apply_patch",
        "project_shell",
        "project_manage",
        "project_code",
        "device_read",
        "device_screenshot",
        "device_desktop",
    ]
    input: dict[str, Any]

    @model_validator(mode="after")
    def validate_tool_input(self) -> OperationPayload:
        adapters: dict[str, TypeAdapter[Any]] = {
            "device_screenshot": TypeAdapter(DeviceScreenshotInput),
            "device_desktop": TypeAdapter(DeviceDesktopInput),
            "device_read": TypeAdapter(DeviceReadInput),
            "project_read": TypeAdapter(ProjectReadInput),
            "project_apply_patch": TypeAdapter(ProjectApplyPatchInput),
            "project_shell": TypeAdapter(ProjectShellInput),
            "project_manage": TypeAdapter(ProjectManageInput),
            "project_code": TypeAdapter(ProjectCodeInput),
        }
        adapters[self.tool_name].validate_python(self.input)
        return self


class TunnelEnvelope(CoditoModel):
    protocol_version: str = PROTOCOL_VERSION
    kind: MessageKind
    message_id: OpaqueId
    correlation_id: OpaqueId | None = None
    sequence: int = Field(ge=0)
    connection_epoch: int = Field(ge=1)
    sent_at: datetime = Field(default_factory=utc_now)
    deadline_at: datetime | None = None
    bindings: TunnelBindings
    action_digest: Sha256 | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("protocol_version")
    @classmethod
    def supported_version(cls, value: str) -> str:
        if value != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version: {value}")
        return value

    @field_validator("sent_at", "deadline_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def deadline_follows_send(self) -> TunnelEnvelope:
        if self.deadline_at is not None and self.deadline_at <= self.sent_at:
            raise ValueError("deadline_at must be later than sent_at")
        operation_kinds = {
            MessageKind.OPERATION,
            MessageKind.OPERATION_RECEIVED,
            MessageKind.OPERATION_STARTED,
            MessageKind.OPERATION_PROGRESS,
            MessageKind.OPERATION_RESULT,
            MessageKind.OPERATION_CANCEL,
            MessageKind.TERMINAL_ACK,
        }
        if self.kind in operation_kinds:
            if self.correlation_id is None or self.correlation_id == self.message_id:
                raise ValueError(
                    "operation lifecycle messages require a correlation_id distinct from message_id"
                )
            if self.action_digest is None:
                raise ValueError("operation lifecycle messages require action_digest")
        if self.kind is MessageKind.OPERATION:
            operation_payload = OperationPayload.model_validate(self.payload)
            expected_digest = compute_action_digest(operation_payload)
            if self.action_digest != expected_digest:
                raise ValueError("action_digest must cover the complete operation payload")
        return self
