"""Input and result models for bounded, non-interactive shell jobs."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .types import MAX_SCRIPT_BYTES, CoditoModel, OpaqueId, RelativePath


class StructuredCommand(CoditoModel):
    kind: Literal["exec"] = Field(
        default="exec", description="Execute without command-shell interpolation."
    )
    executable: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Command name or project-relative executable path; never an absolute local path."
        ),
    )
    arguments: list[str] = Field(
        default_factory=list,
        max_length=256,
        description="Ordered argv values passed without joining.",
    )
    environment: dict[str, str] = Field(
        default_factory=dict,
        max_length=64,
        description="Bounded requested overrides; local policy sanitizes and may reject them.",
    )

    @field_validator("executable")
    @classmethod
    def reject_device_executable(cls, value: str) -> str:
        if "\x00" in value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
            raise ValueError("executable must be a command name or project-relative path")
        return value

    @field_validator("arguments")
    @classmethod
    def bound_arguments(cls, value: list[str]) -> list[str]:
        if sum(len(item.encode("utf-8")) for item in value) > 64 * 1024:
            raise ValueError("combined arguments exceed 64 KiB")
        if any("\x00" in item for item in value):
            raise ValueError("argument contains NUL")
        return value


class ScriptCommand(CoditoModel):
    kind: Literal["script"] = Field(
        default="script", description="Explicitly opt into command-shell parsing."
    )
    shell: Literal["powershell", "cmd"] = Field(description="Exact shell grammar for the script.")
    script: str = Field(
        min_length=1, description="Non-interactive script displayed exactly in local approval UI."
    )

    @field_validator("script")
    @classmethod
    def bound_script(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise ValueError("script contains NUL or exceeds 256 KiB")
        return value


type ShellCommand = Annotated[StructuredCommand | ScriptCommand, Field(discriminator="kind")]


class ShellStartInput(CoditoModel):
    action: Literal["start"] = Field(default="start", description="Start a new bounded shell job.")
    project_id: OpaqueId = Field(description="Opaque ID from list_projects; never a local path.")
    working_directory: RelativePath = Field(
        default="", description="Project-relative working directory; empty means root."
    )
    purpose: str = Field(
        min_length=1,
        max_length=1000,
        description="Short human-readable reason shown in history/UI.",
    )
    timeout_seconds: int = Field(
        default=300, ge=1, le=1800, description="Wall-clock deadline enforced on the process tree."
    )
    output_limit_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1024,
        le=10 * 1024 * 1024,
        description="Combined stdout/stderr cap before truncation/termination policy.",
    )
    idempotency_key: OpaqueId = Field(
        description="Detect duplicate submission; never authorizes automatic uncertain replay."
    )
    command: ShellCommand = Field(
        description="Structured argv or an explicit PowerShell/cmd script."
    )


class ShellPollInput(CoditoModel):
    action: Literal["poll"] = Field(
        default="poll", description="Read job state/output after a cursor."
    )
    project_id: OpaqueId = Field(description="Original job project binding.")
    job_id: OpaqueId = Field(description="Stable identifier returned by start.")
    sequence_cursor: int = Field(
        default=0, ge=0, description="Return chunks with sequence strictly greater than this value."
    )
    wait_milliseconds: int = Field(
        default=0,
        ge=0,
        le=30_000,
        description="Maximum bounded long-poll wait; zero returns immediately.",
    )


class ShellCancelInput(CoditoModel):
    action: Literal["cancel"] = Field(
        default="cancel", description="Request process-tree cancellation."
    )
    project_id: OpaqueId = Field(description="Original job project binding.")
    job_id: OpaqueId = Field(description="Stable identifier returned by start.")
    reason: str = Field(
        default="cancelled by caller",
        min_length=1,
        max_length=500,
        description="Safe concise reason recorded with the cancellation event.",
    )


type ProjectShellInput = Annotated[
    ShellStartInput | ShellPollInput | ShellCancelInput,
    Field(discriminator="action"),
]


class ShellStartResult(CoditoModel):
    action: Literal["start"] = "start"
    project_id: OpaqueId
    job_id: OpaqueId
    state: Literal["pending_approval", "queued", "running", "completed", "denied"]
    connection_epoch: int = Field(ge=1)


class ShellOutputChunk(CoditoModel):
    sequence: int = Field(ge=1)
    stream: Literal["stdout", "stderr", "system"]
    text: str
    truncated: bool = False


class ShellPollResult(CoditoModel):
    action: Literal["poll"] = "poll"
    project_id: OpaqueId
    job_id: OpaqueId
    state: Literal[
        "pending_approval",
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
        "outcome_unknown",
    ]
    chunks: list[ShellOutputChunk]
    next_sequence_cursor: int = Field(ge=0)
    exit_code: int | None = None
    output_truncated: bool = False


class ShellCancelResult(CoditoModel):
    action: Literal["cancel"] = "cancel"
    project_id: OpaqueId
    job_id: OpaqueId
    state: Literal["cancelled", "already_terminal", "not_found"]


type ProjectShellResult = Annotated[
    ShellStartResult | ShellPollResult | ShellCancelResult,
    Field(discriminator="action"),
]
