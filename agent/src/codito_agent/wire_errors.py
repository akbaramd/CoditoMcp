from __future__ import annotations

import re
from typing import Any

from codito_protocol import ErrorCode, ToolError, ToolFailure

from .errors import AgentError

LOCAL_ERROR_MAP: dict[str, ErrorCode] = {
    "action_digest_mismatch": ErrorCode.INVALID_REQUEST,
    "already_enrolled": ErrorCode.INVALID_REQUEST,
    "approval_denied": ErrorCode.APPROVAL_DENIED,
    "approval_expired": ErrorCode.APPROVAL_EXPIRED,
    "binary_file": ErrorCode.ENCODING_UNSUPPORTED,
    "binding_mismatch": ErrorCode.UNAUTHORIZED,
    "browser_capture_failed": ErrorCode.SCREENSHOT_UNAVAILABLE,
    "browser_close_failed": ErrorCode.DESKTOP_UNAVAILABLE,
    "browser_navigation_failed": ErrorCode.DESKTOP_UNAVAILABLE,
    "browser_unavailable": ErrorCode.DESKTOP_UNAVAILABLE,
    "broker_failed": ErrorCode.SANDBOX_UNAVAILABLE,
    "broker_cleanup_failed": ErrorCode.SANDBOX_UNAVAILABLE,
    "broker_protocol_error": ErrorCode.SANDBOX_UNAVAILABLE,
    "broker_unavailable": ErrorCode.SANDBOX_UNAVAILABLE,
    "confirmation_required": ErrorCode.APPROVAL_REQUIRED,
    "capture_too_large": ErrorCode.OUTPUT_LIMIT_EXCEEDED,
    "conflict": ErrorCode.PATCH_CONFLICT,
    "credential_unavailable": ErrorCode.UNAUTHENTICATED,
    "deadline_exceeded": ErrorCode.DEADLINE_EXCEEDED,
    "enrollment_required": ErrorCode.UNAUTHENTICATED,
    "dev_server_failed": ErrorCode.SHELL_NOT_RUNNING,
    "dev_server_stop_failed": ErrorCode.SHELL_NOT_RUNNING,
    "dev_server_timeout": ErrorCode.DEADLINE_EXCEEDED,
    "reenrollment_required": ErrorCode.UNAUTHENTICATED,
    "file_too_large": ErrorCode.FILE_TOO_LARGE,
    "element_not_found": ErrorCode.RESOURCE_MISMATCH,
    "element_not_interactable": ErrorCode.INVALID_REQUEST,
    "frontend_action_failed": ErrorCode.DESKTOP_UNAVAILABLE,
    "frontend_config_required": ErrorCode.INVALID_REQUEST,
    "frontend_port_in_use": ErrorCode.INVALID_REQUEST,
    "frontend_session_closed": ErrorCode.RESOURCE_MISMATCH,
    "frontend_session_exists": ErrorCode.INVALID_REQUEST,
    "frontend_session_expired": ErrorCode.RESOURCE_MISMATCH,
    "frontend_session_not_found": ErrorCode.RESOURCE_MISMATCH,
    "idempotency_conflict": ErrorCode.IDEMPOTENCY_CONFLICT,
    "internal_error": ErrorCode.INTERNAL_ERROR,
    "migration_failed": ErrorCode.INTERNAL_ERROR,
    "screenshot_unavailable": ErrorCode.SCREENSHOT_UNAVAILABLE,
    "desktop_unavailable": ErrorCode.DESKTOP_UNAVAILABLE,
    "invalid_approval": ErrorCode.APPROVAL_DENIED,
    "invalid_config": ErrorCode.INTERNAL_ERROR,
    "invalid_cursor": ErrorCode.INVALID_REQUEST,
    "invalid_frontend_config": ErrorCode.INVALID_REQUEST,
    "invalid_glob": ErrorCode.INVALID_REQUEST,
    "invalid_ipc_request": ErrorCode.INVALID_REQUEST,
    "invalid_patch": ErrorCode.PATCH_INVALID,
    "invalid_path": ErrorCode.PATH_INVALID,
    "invalid_permission": ErrorCode.INVALID_REQUEST,
    "invalid_project": ErrorCode.INVALID_REQUEST,
    "invalid_project_root": ErrorCode.PATH_INVALID,
    "invalid_request": ErrorCode.INVALID_REQUEST,
    "invalid_release": ErrorCode.INTERNAL_ERROR,
    "invalid_setting": ErrorCode.INTERNAL_ERROR,
    "invalid_state": ErrorCode.INTERNAL_ERROR,
    "ipc_unavailable": ErrorCode.INTERNAL_ERROR,
    "job_not_found": ErrorCode.SHELL_NOT_FOUND,
    "login_required": ErrorCode.UNAUTHENTICATED,
    "login_timeout": ErrorCode.UNAUTHENTICATED,
    "not_a_directory": ErrorCode.PATH_INVALID,
    "not_a_file": ErrorCode.PATH_INVALID,
    "not_enrolled": ErrorCode.UNAUTHENTICATED,
    "navigation_blocked": ErrorCode.UNAUTHORIZED,
    "operation_conflict": ErrorCode.UNAUTHORIZED,
    "operation_not_found": ErrorCode.INTERNAL_ERROR,
    "outcome_unknown": ErrorCode.OUTCOME_UNKNOWN,
    "patch_conflict": ErrorCode.PATCH_CONFLICT,
    "path_not_found": ErrorCode.FILE_NOT_FOUND,
    "path_race": ErrorCode.PATH_CHANGED,
    "project_busy": ErrorCode.RATE_LIMITED,
    "project_disabled": ErrorCode.PROJECT_NOT_FOUND,
    "project_exists": ErrorCode.INVALID_REQUEST,
    "project_identity_changed": ErrorCode.PATH_CHANGED,
    "project_not_found": ErrorCode.PROJECT_NOT_FOUND,
    "project_unavailable": ErrorCode.PROJECT_NOT_FOUND,
    "protocol_error": ErrorCode.INVALID_REQUEST,
    "queue_full": ErrorCode.QUEUE_FULL,
    "read_failed": ErrorCode.INTERNAL_ERROR,
    "recovery_failed": ErrorCode.INTERNAL_ERROR,
    "release_unavailable": ErrorCode.INTERNAL_ERROR,
    "relay_protocol_error": ErrorCode.INTERNAL_ERROR,
    "relay_rejected": ErrorCode.UNAUTHORIZED,
    "result_limit": ErrorCode.OUTPUT_LIMIT_EXCEEDED,
    "request_expired": ErrorCode.INVALID_REQUEST,
    "request_not_found": ErrorCode.INVALID_REQUEST,
    "sandbox_policy_denied": ErrorCode.UNAUTHORIZED,
    "sandbox_unavailable": ErrorCode.SANDBOX_UNAVAILABLE,
    "search_timeout": ErrorCode.DEADLINE_EXCEEDED,
    "sensitive_input_blocked": ErrorCode.UNAUTHORIZED,
    "stale_snapshot": ErrorCode.RESOURCE_MISMATCH,
    "stale_connection": ErrorCode.UNAUTHORIZED,
    "ticket_expired": ErrorCode.UNAUTHENTICATED,
    "too_many_sessions": ErrorCode.RATE_LIMITED,
    "unauthorized": ErrorCode.UNAUTHORIZED,
    "unsafe_hardlink": ErrorCode.HARDLINK_MUTATION_FORBIDDEN,
    "unsafe_path": ErrorCode.PATH_OUTSIDE_PROJECT,
    "unsupported_encoding": ErrorCode.ENCODING_UNSUPPORTED,
    "unsupported_platform": ErrorCode.INTERNAL_ERROR,
    "update_check_failed": ErrorCode.INTERNAL_ERROR,
    "update_launch_failed": ErrorCode.INTERNAL_ERROR,
    "update_not_required": ErrorCode.INVALID_REQUEST,
}

_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?:^|\s)[A-Za-z]:[\\/]")


def _safe_details(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _ABSOLUTE_WINDOWS_PATH.search(value) or value.startswith(("\\\\", "//")):
            return "[local path redacted]"
        return value[:1024]
    if isinstance(value, list):
        return [_safe_details(item) for item in value[:64]]
    if isinstance(value, dict):
        return {str(key)[:100]: _safe_details(item) for key, item in list(value.items())[:64]}
    return str(type(value).__name__)


def to_tool_failure(error: AgentError, correlation_id: str | None) -> ToolFailure:
    code = LOCAL_ERROR_MAP.get(error.code, ErrorCode.INTERNAL_ERROR)
    message = error.message[:1024] or "The device operation failed"
    return ToolFailure(
        text=message[:2048],
        error=ToolError(
            code=code,
            message=message,
            retryable=error.retryable,
            correlation_id=correlation_id,
            details=_safe_details(error.details),
        ),
    )
