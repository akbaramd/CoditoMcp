"""Stable, typed error contracts. Error messages must be safe for remote logs."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from .types import CoditoModel, OpaqueId


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHENTICATED = "unauthenticated"
    UNAUTHORIZED = "unauthorized"
    RESOURCE_MISMATCH = "resource_mismatch"
    SCOPE_MISSING = "scope_missing"
    RATE_LIMITED = "rate_limited"
    PROJECT_NOT_FOUND = "project_not_found"
    DEVICE_OFFLINE = "device_offline"
    DEVICE_REVOKED = "device_revoked"
    LINK_REVOKED = "link_revoked"
    QUEUE_FULL = "queue_full"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PATH_INVALID = "path_invalid"
    PATH_OUTSIDE_PROJECT = "path_outside_project"
    PATH_CHANGED = "path_changed"
    REPARSE_POINT_FORBIDDEN = "reparse_point_forbidden"
    HARDLINK_MUTATION_FORBIDDEN = "hardlink_mutation_forbidden"
    FILE_NOT_FOUND = "file_not_found"
    FILE_TOO_LARGE = "file_too_large"
    ENCODING_UNSUPPORTED = "encoding_unsupported"
    HASH_MISMATCH = "hash_mismatch"
    PATCH_INVALID = "patch_invalid"
    PATCH_CONFLICT = "patch_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_EXPIRED = "approval_expired"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    SHELL_NOT_FOUND = "shell_not_found"
    SHELL_NOT_RUNNING = "shell_not_running"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SCREENSHOT_UNAVAILABLE = "screenshot_unavailable"
    DESKTOP_UNAVAILABLE = "desktop_unavailable"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class ToolError(CoditoModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=1024)
    retryable: bool = False
    correlation_id: OpaqueId | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ToolFailure(CoditoModel):
    ok: bool = False
    text: str = Field(min_length=1, max_length=2048)
    error: ToolError
