from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from codito_protocol import (
    MessageKind,
    ProjectApplyPatchInput,
    StructuredCommand,
    TunnelBindings,
    TunnelEnvelope,
    canonical_action_bytes,
    export_schemas,
    schema_for,
    validate_project_apply_patch,
)
from codito_protocol.shell import ScriptCommand
from codito_protocol.types import MAX_PATCH_BYTES, OpaqueId, ProjectGlob, RelativePath, Sha256
from pydantic import TypeAdapter, ValidationError

PROJECT_ID = "project_01J123456789ABCDEF"
ACCOUNT_ID = "account_01J123456789ABCDEF"
DEVICE_ID = "device_01J123456789ABCDEF"
MESSAGE_ID = "message_01J123456789ABCDEF"
IDEMPOTENCY_KEY = "idemkey_01J123456789ABCDEF"
HASH = "a" * 64


@pytest.mark.parametrize(
    ("adapter", "value"),
    [
        (TypeAdapter(OpaqueId), "short"),
        (TypeAdapter(Sha256), "z" * 64),
        (TypeAdapter(RelativePath), "a" * 1025),
        (TypeAdapter(RelativePath), "bad\x00name"),
        (TypeAdapter(RelativePath), "name:stream"),
        (TypeAdapter(RelativePath), "trailing./file"),
        (TypeAdapter(RelativePath), "COM1.log"),
        (TypeAdapter(ProjectGlob), "/**/*.py"),
        (TypeAdapter(ProjectGlob), "src//*.py"),
        (TypeAdapter(ProjectGlob), "a" * 513),
        (TypeAdapter(ProjectGlob), "src\\*.py"),
    ],
)
def test_primitive_validation_edges(adapter: TypeAdapter[str], value: str) -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


def test_root_alias_and_valid_glob_normalize() -> None:
    assert TypeAdapter(RelativePath).validate_python(".") == ""
    assert TypeAdapter(ProjectGlob).validate_python("src/**/*.py") == "src/**/*.py"
    assert TypeAdapter(Sha256).validate_python("A" * 64) == "a" * 64


def patch_input(patch: str, hashes: dict[str, str | None]) -> dict[str, object]:
    return {
        "project_id": PROJECT_ID,
        "patch": patch,
        "base_hashes": hashes,
        "idempotency_key": IDEMPOTENCY_KEY,
    }


@pytest.mark.parametrize(
    "value",
    [
        patch_input(
            "*** Begin Patch\n*** Update File: same.txt\n@@\n-a\n+b\n"
            "*** Update File: same.txt\n@@\n-b\n+c\n*** End Patch\n",
            {"same.txt": HASH},
        ),
        patch_input(
            "*** Begin Patch\n*** Add File: a.txt\n*** Move to: b.txt\n+x\n*** End Patch\n",
            {"a.txt": None, "b.txt": None},
        ),
        patch_input(
            "*** Begin Patch\n*** Update File: a.txt\n\n*** Move to: b.txt\n"
            "@@\n-a\n+b\n*** End Patch\n",
            {"a.txt": HASH, "b.txt": None},
        ),
        patch_input(
            "*** Begin Patch\n*** Update File: a.txt\n*** Move to: a.txt\n@@\n-a\n+b\n"
            "*** End Patch\n",
            {"a.txt": HASH},
        ),
        patch_input(
            "*** Begin Patch\nthis is not a section at all\n*** End Patch\n",
            {"a.txt": HASH},
        ),
        patch_input(
            "*** Begin Patch\n*** Add File: a.txt\n+x\n*** End Patch\n",
            {"a.txt": HASH},
        ),
        patch_input(
            "*** Begin Patch\n*** Delete File: a.txt\n*** End Patch\n",
            {"a.txt": None},
        ),
    ],
)
def test_patch_rejects_invalid_precondition_shapes(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProjectApplyPatchInput.model_validate(value)


@pytest.mark.parametrize(
    "patch",
    [
        "x" * (MAX_PATCH_BYTES + 1),
        "not anchored at the beginning and long enough to parse",
        "*** Begin Patch\n*** Add File: a.txt\n+x\nmissing end marker",
        "*** Begin Patch\n*** Add File: a.txt\n+x\x00\n*** End Patch\n",
    ],
    ids=["too-large", "missing-begin", "missing-end", "nul"],
)
def test_patch_document_bounds(patch: str) -> None:
    with pytest.raises(ValidationError):
        ProjectApplyPatchInput.model_validate(patch_input(patch, {"a.txt": None}))


def test_validate_patch_adapter() -> None:
    value = validate_project_apply_patch(
        patch_input(
            "*** Begin Patch\n*** Delete File: a.txt\n*** End Patch\n",
            {"a.txt": HASH},
        )
    )
    assert value.project_id == PROJECT_ID


@pytest.mark.parametrize(
    "command",
    [
        {"kind": "exec", "executable": "\\\\server\\tool.exe"},
        {"kind": "exec", "executable": "C:/Windows/System32/cmd.exe"},
        {"kind": "exec", "executable": "tool", "arguments": ["bad\x00arg"]},
        {"kind": "exec", "executable": "tool", "arguments": ["x" * (64 * 1024 + 1)]},
        {"kind": "script", "shell": "powershell", "script": "bad\x00script"},
        {"kind": "script", "shell": "cmd", "script": "x" * (256 * 1024 + 1)},
    ],
    ids=[
        "unc-executable",
        "absolute-executable",
        "nul-argument",
        "arguments-too-large",
        "nul-script",
        "script-too-large",
    ],
)
def test_shell_command_bounds(command: dict[str, object]) -> None:
    model = StructuredCommand if command["kind"] == "exec" else ScriptCommand
    with pytest.raises(ValidationError):
        model.model_validate(command)


def test_valid_shell_commands() -> None:
    assert StructuredCommand(executable="uv", arguments=["run", "pytest"]).kind == "exec"
    assert ScriptCommand(shell="powershell", script="git status").kind == "script"


def bindings() -> TunnelBindings:
    return TunnelBindings(account_id=ACCOUNT_ID, device_id=DEVICE_ID)


def test_envelope_version_timestamp_and_deadline_guards() -> None:
    sent_at = datetime.now(UTC)
    common = {
        "kind": MessageKind.HEARTBEAT,
        "message_id": MESSAGE_ID,
        "sequence": 0,
        "connection_epoch": 1,
        "bindings": bindings(),
    }
    with pytest.raises(ValidationError, match="unsupported protocol"):
        TunnelEnvelope.model_validate({**common, "protocol_version": "2.0"})
    with pytest.raises(ValidationError, match="UTC offset"):
        naive = datetime(2026, 9, 8)  # noqa: DTZ001 - deliberately invalid protocol input
        TunnelEnvelope.model_validate({**common, "sent_at": naive})
    with pytest.raises(ValidationError, match="later than sent_at"):
        TunnelEnvelope.model_validate(
            {**common, "sent_at": sent_at, "deadline_at": sent_at - timedelta(seconds=1)}
        )

    valid = TunnelEnvelope.model_validate({**common, "protocol_version": "1.0"})
    assert valid.deadline_at is None


def test_canonical_digest_supports_models_enums_dates_and_sequences() -> None:
    value = {
        "bindings": bindings(),
        "kind": MessageKind.HELLO,
        "date": datetime(2026, 9, 8, tzinfo=UTC),
        "items": [1, 2, 3],
    }
    rendered = canonical_action_bytes(value)
    assert b'"kind":"hello"' in rendered
    assert b'"items":[1,2,3]' in rendered


def test_schema_errors_and_export() -> None:
    with pytest.raises(KeyError, match="unknown Codito schema"):
        schema_for("not-real")
    written = export_schemas(Path(__file__).resolve().parents[1] / "schemas")
    assert len(written) == 94
    assert {
        "facade-file-read-output.schema.json",
        "facade-execute-shell-output.schema.json",
        "facade-screenshot-capture-output.schema.json",
        "facade-code-definition-output.schema.json",
        "facade-code-reindex-output.schema.json",
        "facade-frontend-session-start-input.schema.json",
        "facade-frontend-snapshot-output.schema.json",
        "facade-frontend-inspect-output.schema.json",
        "facade-frontend-act-input.schema.json",
        "facade-frontend-source-output.schema.json",
        "facade-frontend-session-stop-output.schema.json",
        "project-frontend-input.schema.json",
        "project-frontend-result.schema.json",
    }.issubset({path.name for path in written})
    assert all(path.read_text(encoding="utf-8").endswith("\n") for path in written)
