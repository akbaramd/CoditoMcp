"""Input and result models for the project_read MCP tool."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .device_read import normalize_read_scope
from .types import CoditoModel, OpaqueId, ProjectGlob, RelativePath, Sha256


class ListProjectsInput(CoditoModel):
    operation: Literal["list_projects"] = Field(
        description="List projects authorized for the current OAuth device link."
    )
    cursor: str | None = Field(
        default=None, max_length=512, description="Opaque continuation returned by this operation."
    )
    limit: int = Field(default=50, ge=1, le=100, description="Maximum projects to return.")


class ScopedReadInput(CoditoModel):
    scope_path: str | None = Field(
        default=None,
        description="Requested external Windows directory; local approval is required.",
    )
    purpose: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("scope_path")
    @classmethod
    def normalized_scope(cls, value: str | None) -> str | None:
        return normalize_read_scope(value) if value is not None else None

    @model_validator(mode="after")
    def external_scope_requires_purpose(self) -> ScopedReadInput:
        if self.scope_path is not None and self.purpose is None:
            raise ValueError("External reads require a purpose for local approval")
        return self


class ListDirectoryInput(ScopedReadInput):
    operation: Literal["list_directory"] = Field(
        description="List bounded metadata below a project-relative directory."
    )
    project_id: OpaqueId = Field(description="Opaque ID from list_projects; never a local path.")
    path: RelativePath = Field(
        default="", description="Forward-slash project-relative directory; empty means root."
    )
    glob: ProjectGlob = Field(
        default="*", description="Project-relative filename glob applied below path."
    )
    recursive: bool = Field(default=False, description="Whether to traverse subdirectories.")
    cursor: str | None = Field(
        default=None, max_length=512, description="Opaque continuation from the same query."
    )
    limit: int = Field(default=200, ge=1, le=1000, description="Maximum entries to return.")


class ReadFileInput(ScopedReadInput):
    operation: Literal["read_file"] = Field(description="Read a bounded, numbered file range.")
    project_id: OpaqueId = Field(description="Opaque ID from list_projects; never a local path.")
    path: RelativePath = Field(description="Forward-slash path relative to the project root.")
    start_line: int = Field(default=1, ge=1, description="One-based first line to return.")
    max_lines: int | None = Field(
        default=None,
        ge=1,
        le=2000,
        description="Optional per-page line cap, also enforced when resuming continuation.",
    )
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="Inclusive one-based last line, or null for byte-bounded read.",
    )
    max_bytes: int = Field(
        default=256 * 1024,
        ge=1,
        le=2 * 1024 * 1024,
        description="Hard maximum content bytes to inspect/return for this page.",
    )
    continuation: str | None = Field(
        default=None,
        max_length=512,
        description="Opaque cursor returned by the same file read; do not alter it.",
    )


class SearchTextInput(ScopedReadInput):
    operation: Literal["search_text"] = Field(description="Search bounded text inside a project.")
    project_id: OpaqueId = Field(description="Opaque ID from list_projects; never a local path.")
    query: str = Field(min_length=1, max_length=4096, description="Literal text or regex to find.")
    path: RelativePath = Field(
        default="", description="Project-relative subtree to search; empty means root."
    )
    glob: ProjectGlob = Field(
        default="**/*", description="Project-relative file glob within the selected subtree."
    )
    case_sensitive: bool = Field(default=False, description="Use case-sensitive matching.")
    regex: bool = Field(
        default=False, description="Interpret query as a bounded regular expression."
    )
    max_results: int = Field(
        default=100, ge=1, le=500, description="Maximum matching locations to return."
    )
    max_bytes_per_match: int = Field(
        default=2048,
        ge=128,
        le=16 * 1024,
        description="Maximum UTF-8 preview bytes per matching location.",
    )
    continuation: str | None = Field(
        default=None, max_length=512, description="Opaque continuation from the identical query."
    )


type ProjectReadInput = Annotated[
    ListProjectsInput | ListDirectoryInput | ReadFileInput | SearchTextInput,
    Field(discriminator="operation"),
]


class Continuation(CoditoModel):
    cursor: str
    remaining_estimate: int | None = Field(default=None, ge=0)


class ProjectSummary(CoditoModel):
    project_id: OpaqueId
    title: str = Field(min_length=1, max_length=200)
    device_id: OpaqueId
    mode: Literal["isolated", "native_approval", "native_trusted", "native_project", "full_access"]
    online: bool


class ListProjectsResult(CoditoModel):
    operation: Literal["list_projects"] = "list_projects"
    projects: list[ProjectSummary]
    continuation: Continuation | None = None


class DirectoryEntry(CoditoModel):
    path: RelativePath
    kind: Literal["file", "directory"]
    size: int | None = Field(default=None, ge=0)
    sha256: Sha256 | None = None


class ListDirectoryResult(CoditoModel):
    operation: Literal["list_directory"] = "list_directory"
    project_id: OpaqueId
    path: RelativePath
    entries: list[DirectoryEntry]
    continuation: Continuation | None = None


class ReadFileResult(CoditoModel):
    operation: Literal["read_file"] = "read_file"
    project_id: OpaqueId
    path: RelativePath
    numbered_text: str
    encoding: Literal["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "binary"]
    newline: Literal["lf", "crlf", "cr", "mixed", "none"]
    size: int = Field(ge=0)
    sha256: Sha256
    first_line: int = Field(ge=1)
    last_line: int = Field(ge=0)
    truncated: bool
    continuation: Continuation | None = None


class SearchMatch(CoditoModel):
    path: RelativePath
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    preview: str


class SearchTextResult(CoditoModel):
    operation: Literal["search_text"] = "search_text"
    project_id: OpaqueId
    matches: list[SearchMatch]
    truncated: bool
    continuation: Continuation | None = None


type ProjectReadResult = Annotated[
    ListProjectsResult | ListDirectoryResult | ReadFileResult | SearchTextResult,
    Field(discriminator="operation"),
]
