from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from codito_protocol import (
    TOOL_CONTRACTS,
    MessageKind,
    ProjectApplyPatchInput,
    ProjectReadInput,
    TunnelBindings,
    TunnelEnvelope,
    compute_action_digest,
    schema_for,
    validate_project_manage,
    validate_project_read,
    validate_project_shell,
)
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

PROJECT_ID = "project_01J123456789ABCDEF"
ACCOUNT_ID = "account_01J123456789ABCDEF"
DEVICE_ID = "device_01J123456789ABCDEF"
MESSAGE_ID = "message_01J123456789ABCDEF"
IDEMPOTENCY_KEY = "idemkey_01J123456789ABCDEF"
HASH = "0" * 64


def test_exactly_seven_tools_are_public() -> None:
    assert set(TOOL_CONTRACTS) == {
        "project_read",
        "project_apply_patch",
        "project_shell",
        "project_manage",
        "device_read",
        "device_screenshot",
        "device_desktop",
    }
    assert TOOL_CONTRACTS["project_read"]["securitySchemes"][0]["scopes"] == [
        "projects:read",
        "files:read",
    ]
    assert TOOL_CONTRACTS["project_apply_patch"]["securitySchemes"][0]["scopes"] == [
        "projects:read",
        "files:read",
        "files:write",
    ]
    assert TOOL_CONTRACTS["project_shell"]["securitySchemes"][0]["scopes"] == [
        "projects:read",
        "shell:execute",
    ]
    assert TOOL_CONTRACTS["project_manage"]["securitySchemes"][0]["scopes"] == [
        "projects:read",
        "projects:write",
    ]


def test_project_management_never_accepts_a_local_path_or_approval() -> None:
    request = validate_project_manage(
        {
            "operation": "request_add_project",
            "title": "Example",
            "idempotency_key": IDEMPOTENCY_KEY,
        }
    )
    assert request.operation == "request_add_project"
    with pytest.raises(ValidationError):
        validate_project_manage(
            {
                "operation": "request_add_project",
                "title": "Example",
                "idempotency_key": IDEMPOTENCY_KEY,
                "absolute_path": r"C:\source",
            }
        )


def test_read_discriminator_and_unknown_fields() -> None:
    value = validate_project_read(
        {"operation": "read_file", "project_id": PROJECT_ID, "path": "src/main.py"}
    )
    assert value.operation == "read_file"

    with pytest.raises(ValidationError):
        TypeAdapter(ProjectReadInput).validate_python(
            {
                "operation": "read_file",
                "project_id": PROJECT_ID,
                "path": "src/main.py",
                "approved": True,
            }
        )


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "/Windows/System32/config/SAM",
        "C:/Windows/win.ini",
        "folder\\file.txt",
        "folder//file.txt",
        "file.txt:stream",
        "NUL.txt",
        "folder./file.txt",
    ],
)
def test_protocol_rejects_unsafe_path_spellings(path: str) -> None:
    with pytest.raises(ValidationError):
        validate_project_read({"operation": "read_file", "project_id": PROJECT_ID, "path": path})


def test_patch_requires_anchors_and_base_hashes() -> None:
    value = ProjectApplyPatchInput.model_validate(
        {
            "project_id": PROJECT_ID,
            "patch": "*** Begin Patch\n*** Update File: README.md\n@@\n-old\n+new\n*** End Patch\n",
            "base_hashes": {"README.md": HASH},
            "idempotency_key": IDEMPOTENCY_KEY,
            "dry_run": True,
        }
    )
    assert value.dry_run is True

    add = ProjectApplyPatchInput.model_validate(
        {
            "project_id": PROJECT_ID,
            "patch": "*** Begin Patch\n*** Add File: new.txt\n+new\n*** End Patch\n",
            "base_hashes": {"new.txt": None},
            "idempotency_key": IDEMPOTENCY_KEY,
        }
    )
    assert add.base_hashes["new.txt"] is None

    move = ProjectApplyPatchInput.model_validate(
        {
            "project_id": PROJECT_ID,
            "patch": (
                "*** Begin Patch\n*** Update File: old.txt\n*** Move to: new.txt\n"
                "@@\n-old\n+new\n*** End Patch\n"
            ),
            "base_hashes": {"old.txt": HASH, "new.txt": None},
            "idempotency_key": "idemkey_01J123456789ABCDEG",
        }
    )
    assert move.base_hashes == {"old.txt": HASH, "new.txt": None}

    with pytest.raises(ValidationError, match="exactly cover"):
        ProjectApplyPatchInput.model_validate(
            {
                "project_id": PROJECT_ID,
                "patch": "*** Begin Patch\n*** Add File: new.txt\n+new\n*** End Patch\n",
                "base_hashes": {"other.txt": None},
                "idempotency_key": IDEMPOTENCY_KEY,
            }
        )

    with pytest.raises(ValidationError):
        ProjectApplyPatchInput.model_validate(
            {
                "project_id": PROJECT_ID,
                "patch": "--- README.md\n+++ README.md\n",
                "base_hashes": {"README.md": HASH},
                "idempotency_key": IDEMPOTENCY_KEY,
            }
        )


def test_shell_poll_keeps_project_binding() -> None:
    request = validate_project_shell(
        {
            "action": "poll",
            "project_id": PROJECT_ID,
            "job_id": "shelljob_01J123456789ABCDEF",
            "sequence_cursor": 4,
        }
    )
    assert request.project_id == PROJECT_ID
    assert request.sequence_cursor == 4


def test_shell_start_defaults_to_bounded_interaction_wait() -> None:
    request = validate_project_shell(
        {
            "action": "start",
            "project_id": PROJECT_ID,
            "purpose": "Run approved tool",
            "idempotency_key": IDEMPOTENCY_KEY,
            "command": {"kind": "exec", "executable": "git", "arguments": ["status"]},
        }
    )
    assert request.start_wait_milliseconds == 30_000

    with pytest.raises(ValidationError):
        validate_project_shell(
            {
                "action": "start",
                "project_id": PROJECT_ID,
                "purpose": "Run approved tool",
                "idempotency_key": IDEMPOTENCY_KEY,
                "start_wait_milliseconds": 30_001,
                "command": {"kind": "exec", "executable": "git", "arguments": ["status"]},
            }
        )


def test_operation_envelope_requires_digest() -> None:
    sent_at = datetime.now(UTC)
    with pytest.raises(ValidationError):
        TunnelEnvelope(
            kind=MessageKind.OPERATION,
            message_id=MESSAGE_ID,
            correlation_id="operation_01J123456789ABCDEF",
            sequence=1,
            connection_epoch=1,
            sent_at=sent_at,
            deadline_at=sent_at + timedelta(seconds=30),
            bindings=TunnelBindings(account_id=ACCOUNT_ID, device_id=DEVICE_ID),
            payload={"tool_name": "project_read", "input": {"operation": "list_projects"}},
        )


def test_tunnel_has_received_started_progress_and_result_lifecycle() -> None:
    assert {
        MessageKind.OPERATION_RECEIVED,
        MessageKind.OPERATION_STARTED,
        MessageKind.OPERATION_PROGRESS,
        MessageKind.OPERATION_RESULT,
    } <= set(MessageKind)


def test_operation_digest_covers_tool_name_and_complete_input() -> None:
    payload = {"tool_name": "project_read", "input": {"operation": "list_projects"}}
    envelope = TunnelEnvelope(
        kind=MessageKind.OPERATION,
        message_id=MESSAGE_ID,
        correlation_id="operation_01J123456789ABCDEF",
        sequence=1,
        connection_epoch=1,
        bindings=TunnelBindings(account_id=ACCOUNT_ID, device_id=DEVICE_ID),
        action_digest=compute_action_digest(payload),
        payload=payload,
    )
    assert envelope.correlation_id != envelope.message_id

    tampered = {"tool_name": "project_read", "input": {"operation": "list_projects", "limit": 1}}
    with pytest.raises(ValidationError, match="complete operation payload"):
        TunnelEnvelope(
            **{
                **envelope.model_dump(),
                "payload": tampered,
            }
        )


def test_operation_response_requires_distinct_correlation_id_and_digest() -> None:
    with pytest.raises(ValidationError, match="distinct from message_id"):
        TunnelEnvelope(
            kind=MessageKind.OPERATION_RESULT,
            message_id=MESSAGE_ID,
            correlation_id=MESSAGE_ID,
            sequence=2,
            connection_epoch=1,
            bindings=TunnelBindings(account_id=ACCOUNT_ID, device_id=DEVICE_ID),
            action_digest=HASH,
            payload={"ok": True},
        )


@given(st.dictionaries(st.text(min_size=1, max_size=10), st.integers(), max_size=10))
def test_action_digest_is_order_independent(value: dict[str, int]) -> None:
    assert compute_action_digest(value) == compute_action_digest(
        dict(reversed(list(value.items())))
    )


def test_schema_uses_discriminators() -> None:
    read_schema = schema_for("project-read-input")
    shell_schema = schema_for("project-shell-input")
    assert read_schema["discriminator"]["propertyName"] == "operation"
    assert shell_schema["discriminator"]["propertyName"] == "action"
