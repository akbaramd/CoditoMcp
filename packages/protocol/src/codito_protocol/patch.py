"""Input and result models for strict anchored patch application."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .device_read import normalize_read_scope
from .types import MAX_PATCH_BYTES, CoditoModel, OpaqueId, RelativePath, Sha256


class ProjectApplyPatchInput(CoditoModel):
    project_id: OpaqueId = Field(description="Opaque ID from list_projects; never a local path.")
    scope_path: str | None = Field(
        default=None, description="Requested external Windows directory; never proof of approval."
    )
    purpose: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("scope_path")
    @classmethod
    def normalized_scope(cls, value: str | None) -> str | None:
        return normalize_read_scope(value) if value is not None else None

    patch: str = Field(
        min_length=35,
        description="Exact UTF-8 document using the Codito anchored Begin Patch v1 grammar.",
    )
    # ``None`` is an explicit non-existence precondition for an Add destination.
    # Every touched source/destination path must appear in this map.
    base_hashes: dict[RelativePath, Sha256 | None] = Field(
        min_length=1,
        max_length=256,
        description=(
            "Exact precondition for every touched source/destination: SHA-256 means the file "
            "must match; null means the path must not exist."
        ),
    )
    idempotency_key: OpaqueId = Field(
        description="Caller-generated stable key for this exact patch attempt."
    )
    dry_run: bool = Field(
        default=False,
        description="Preflight every check and report results without committing writes.",
    )

    @field_validator("patch")
    @classmethod
    def validate_patch_document(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ValueError("patch exceeds the 2 MiB protocol limit")
        if not value.startswith("*** Begin Patch\n"):
            raise ValueError("patch must start with '*** Begin Patch'")
        if not value.rstrip("\n").endswith("*** End Patch"):
            raise ValueError("patch must end with '*** End Patch'")
        if "\x00" in value:
            raise ValueError("patch contains NUL")
        return value

    @model_validator(mode="after")
    def external_scope_requires_purpose(self) -> ProjectApplyPatchInput:
        if self.scope_path is not None and self.purpose is None:
            raise ValueError("External patches require a purpose for local approval")
        return self

    @model_validator(mode="after")
    def validate_complete_preconditions(self) -> ProjectApplyPatchInput:
        path_adapter = TypeAdapter(RelativePath)
        expectations: dict[str, bool] = {}
        current_section: str | None = None
        for raw_line in self.patch.splitlines():
            section = re.fullmatch(r"\*\*\* (Add|Update|Delete) File: (.+)", raw_line)
            if section:
                operation, raw_path = section.groups()
                path = path_adapter.validate_python(raw_path)
                if path in expectations:
                    raise ValueError(f"path appears in more than one patch section: {path}")
                expectations[path] = operation == "Add"
                current_section = operation
                continue
            move = re.fullmatch(r"\*\*\* Move to: (.+)", raw_line)
            if move:
                if current_section != "Update":
                    raise ValueError("Move to is valid only immediately after an Update header")
                destination = path_adapter.validate_python(move.group(1))
                if destination in expectations:
                    raise ValueError(f"move destination duplicates a touched path: {destination}")
                expectations[destination] = True
                current_section = None
                continue
            # A move directive is valid only on the line immediately following an
            # Update header. Any other line consumes that opportunity.
            if current_section == "Update":
                current_section = None

        if not expectations:
            raise ValueError("patch contains no file sections")
        if set(expectations) != set(self.base_hashes):
            missing = sorted(set(expectations) - set(self.base_hashes))
            extra = sorted(set(self.base_hashes) - set(expectations))
            raise ValueError(
                f"base_hashes must exactly cover touched paths; missing={missing}, extra={extra}"
            )
        for path, must_not_exist in expectations.items():
            digest = self.base_hashes[path]
            if must_not_exist and digest is not None:
                raise ValueError(f"new path must use a null non-existence precondition: {path}")
            if not must_not_exist and digest is None:
                raise ValueError(f"existing source path requires a SHA-256 precondition: {path}")
        return self


class PatchConflict(CoditoModel):
    path: RelativePath
    section_index: int = Field(ge=0)
    hunk_index: int | None = Field(default=None, ge=0)
    code: Literal[
        "base_hash_missing",
        "base_hash_mismatch",
        "context_not_found",
        "context_ambiguous",
        "path_exists",
        "path_missing",
        "destination_exists",
        "invalid_section",
        "policy_denied",
    ]
    message: str = Field(min_length=1, max_length=1024)


class PatchedFile(CoditoModel):
    operation: Literal["add", "update", "delete", "move"]
    path: RelativePath
    destination: RelativePath | None = None
    old_sha256: Sha256 | None = None
    new_sha256: Sha256 | None = None


class ProjectApplyPatchResult(CoditoModel):
    project_id: OpaqueId
    idempotency_key: OpaqueId
    dry_run: bool
    applied: bool
    files: list[PatchedFile] = Field(default_factory=list)
    conflicts: list[PatchConflict] = Field(default_factory=list)
    journal_id: OpaqueId | None = None
