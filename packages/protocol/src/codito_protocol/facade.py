"""Focused MCP inputs mapped to the existing, versioned device wire operations.

Project IDs select the locally configured authority for each call, not an ambient
active-project session. A scope request is never evidence of Windows approval.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .code import (
    DiagnosticSeverity,
    HierarchyDirection,
    ReindexScope,
    SymbolKind,
    TypeHierarchyDirection,
)
from .desktop_action import DeviceDesktopInput
from .device_read import normalize_read_scope
from .patch import ProjectApplyPatchInput
from .screenshot import DisplaySelector
from .shell import ScriptCommand
from .types import CoditoModel, OpaqueId, ProjectGlob, RelativePath, Sha256

Purpose = Annotated[str, Field(min_length=1, max_length=1000)]
Cursor = Annotated[str | None, Field(max_length=512)]
_FIRST_PRINTABLE = 32


class ProjectsListInput(CoditoModel):
    cursor: Cursor = None
    limit: int = Field(default=50, ge=1, le=100)


class ProjectAddInput(CoditoModel):
    title: str = Field(min_length=1, max_length=120)
    idempotency_key: OpaqueId


class ProjectRenameInput(ProjectAddInput):
    project_id: OpaqueId


class ProjectRemoveInput(CoditoModel):
    project_id: OpaqueId
    idempotency_key: OpaqueId


class ProjectTarget(CoditoModel):
    project_id: OpaqueId = Field(description="Origin project ID from projects_list.")
    scope_path: str | None = Field(
        default=None,
        description=(
            "Optional requested absolute Windows directory outside the project, e.g. C:/Temp. "
            "Local project policy and Windows approval still apply; not a trust assertion."
        ),
    )
    purpose: Purpose = Field(description="Reason shown in local approval/history.")

    @field_validator("scope_path")
    @classmethod
    def normalize_scope(cls, value: str | None) -> str | None:
        return normalize_read_scope(value) if value is not None else None


class FileReadInput(ProjectTarget):
    path: RelativePath = Field(description="File path relative to project root or scope_path.")
    start_line: int = Field(default=1, ge=1, le=1_000_000)
    max_lines: int = Field(default=200, ge=1, le=2000)
    max_bytes: int = Field(default=65536, ge=256, le=262144)
    continuation: Cursor = None

    @model_validator(mode="after")
    def valid_file(self) -> FileReadInput:
        if not self.path:
            raise ValueError("A nonempty file path is required")
        return self


class DirectoryListInput(ProjectTarget):
    path: RelativePath = ""
    glob: ProjectGlob = "*"
    recursive: bool = False
    cursor: Cursor = None
    limit: int = Field(default=100, ge=1, le=500)


class TextSearchInput(ProjectTarget):
    query: str = Field(min_length=1, max_length=4096)
    path: RelativePath = ""
    glob: ProjectGlob = "**/*"
    case_sensitive: bool = False
    regex: bool = False
    max_results: int = Field(default=100, ge=1, le=500)
    max_bytes_per_match: int = Field(default=2048, ge=128, le=16384)
    continuation: Cursor = None


class FilePatchInput(ProjectTarget):
    patch: str = Field(min_length=35, description="Exact anchored *** Begin Patch document.")
    base_hashes: dict[RelativePath, Sha256 | None] = Field(min_length=1, max_length=256)
    idempotency_key: OpaqueId
    dry_run: bool = False

    @model_validator(mode="after")
    def valid_patch(self) -> FilePatchInput:
        # Reuse exact grammar, byte bounds, and complete preconditions; never
        # introduce a weaker public parser than the existing wire contract.
        ProjectApplyPatchInput.model_validate(self.model_dump(exclude={"scope_path", "purpose"}))
        return self


class FileDeleteInput(ProjectTarget):
    path: RelativePath
    base_hash: Sha256 = Field(description="Current SHA-256 returned by file_read.")
    idempotency_key: OpaqueId
    dry_run: bool = False

    @field_validator("path")
    @classmethod
    def nonempty_patch_path(cls, value: str) -> str:
        if not value or any(ord(character) < _FIRST_PRINTABLE for character in value):
            raise ValueError("A nonempty file path without control characters is required")
        return value


class ExecuteShellInput(CoditoModel):
    project_id: OpaqueId
    command: str = Field(min_length=1, description="Exact noninteractive command/script to run.")
    executor: Literal["powershell", "cmd"] = "powershell"
    purpose: Purpose
    idempotency_key: OpaqueId
    cwd: str = Field(
        default="",
        description="Project-relative cwd (empty=root), or absolute requested Windows directory.",
    )
    timeout_seconds: int = Field(default=300, ge=1, le=1800)
    output_limit_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    approval_timeout_seconds: int = Field(default=180, ge=15, le=300)
    start_wait_milliseconds: int = Field(default=30000, ge=0, le=30000)
    requested_external_paths: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("command")
    @classmethod
    def bounded_command(cls, value: str) -> str:
        return ScriptCommand(shell="powershell", script=value).script  # noqa: S604 - data model only.

    @field_validator("cwd")
    @classmethod
    def normalize_cwd(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if re.match(r"^[A-Za-z]:", normalized):
            return normalize_read_scope(normalized)
        return str(TypeAdapter(RelativePath).validate_python(normalized))

    @field_validator("requested_external_paths")
    @classmethod
    def normalize_declared_paths(cls, value: list[str]) -> list[str]:
        return [normalize_read_scope(path) for path in value]


class ShellStatusInput(CoditoModel):
    project_id: OpaqueId
    job_id: OpaqueId
    sequence_cursor: int = Field(default=0, ge=0)
    wait_milliseconds: int = Field(default=30000, ge=0, le=30000)


class ShellCancelInput(CoditoModel):
    project_id: OpaqueId
    job_id: OpaqueId
    reason: str = Field(default="cancelled by caller", min_length=1, max_length=500)


class ScreenListInput(CoditoModel):
    purpose: Purpose = "List available Windows screens without capturing pixels"


class ScreenshotCaptureInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    display: DisplaySelector = "primary"
    max_dimension: int = Field(default=1600, ge=640, le=2048)


class BrowserOpenInput(CoditoModel):
    project_id: OpaqueId
    url: str = Field(description="HTTP/HTTPS destination; no credentials or custom schemes.")
    purpose: Purpose
    browser: Literal["default", "firefox"] = "default"

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return DeviceDesktopInput(url=value, purpose="Validate browser destination").url


# ---------------------------------------------------------------------------
# Code intelligence public inputs (language-neutral; 1-based lines, Unicode scalars)
# ---------------------------------------------------------------------------


class CodeStatusInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose = "Query code intelligence status"


class CodeSummaryInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose = "Summarise workspace"


class CodeSymbolSearchFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    query: str = Field(min_length=1, max_length=512)
    scope: Literal["workspace", "document"] = "workspace"
    path: RelativePath | None = None
    kinds: list[SymbolKind] = Field(default_factory=list, max_length=27)
    max_results: int = Field(default=50, ge=1, le=500)


class CodeDefinitionFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    character: int = Field(ge=1, le=100_000)


class CodeReferencesFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    character: int = Field(ge=1, le=100_000)
    include_declaration: bool = True
    max_results: int = Field(default=100, ge=1, le=1000)


class CodeImplementationsFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    character: int = Field(ge=1, le=100_000)
    max_results: int = Field(default=100, ge=1, le=500)


class CodeDiagnosticsFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    severity_min: DiagnosticSeverity = DiagnosticSeverity.HINT
    max_results: int = Field(default=200, ge=1, le=1000)


class CodeHoverFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    character: int = Field(ge=1, le=100_000)


class CodeContextFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    character: int = Field(ge=1, le=100_000)
    source_context_lines: int = Field(default=5, ge=0, le=50)
    include_references: bool = True
    include_diagnostics: bool = True


class CodeCallHierarchyFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    character: int = Field(ge=1, le=100_000)
    direction: HierarchyDirection = HierarchyDirection.BOTH
    max_depth: int = Field(default=3, ge=1, le=10)
    max_nodes: int = Field(default=50, ge=1, le=200)


class CodeTypeHierarchyFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    character: int = Field(ge=1, le=100_000)
    direction: TypeHierarchyDirection = TypeHierarchyDirection.BOTH
    max_depth: int = Field(default=3, ge=1, le=10)
    max_nodes: int = Field(default=50, ge=1, le=200)


class CodeImpactFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    line: int | None = Field(default=None, ge=1, le=10_000_000)
    character: int | None = Field(default=None, ge=1, le=100_000)
    max_nodes: int = Field(default=50, ge=1, le=500)


class CodeArchitectureFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    max_packages: int = Field(default=100, ge=1, le=500)


class CodeDependenciesFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath | None = None
    direction: Literal["all", "direct", "transitive"] = "direct"
    max_results: int = Field(default=100, ge=1, le=500)


class CodeRelatedTestsFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose
    path: RelativePath
    max_results: int = Field(default=50, ge=1, le=200)


class CodeReindexFacadeInput(CoditoModel):
    project_id: OpaqueId
    purpose: Purpose = "Reindex project"
    scope: ReindexScope = ReindexScope.INCREMENTAL
    timeout_seconds: int = Field(default=30, ge=5, le=30)


_CODE_FACADE_TO_OPERATION: dict[str, str] = {
    "code_intelligence_status": "code_intelligence_status",
    "code_workspace_summary": "code_workspace_summary",
    "code_symbol_search": "code_symbol_search",
    "code_definition": "code_definition",
    "code_references": "code_references",
    "code_implementations": "code_implementations",
    "code_diagnostics": "code_diagnostics",
    "code_hover": "code_hover",
    "code_context": "code_context",
    "code_call_hierarchy": "code_call_hierarchy",
    "code_type_hierarchy": "code_type_hierarchy",
    "code_impact": "code_impact",
    "code_architecture": "code_architecture",
    "code_dependencies": "code_dependencies",
    "code_related_tests": "code_related_tests",
    "code_reindex": "code_reindex",
}


FACADE_MODELS: dict[str, type[CoditoModel]] = {
    "projects_list": ProjectsListInput,
    "project_add": ProjectAddInput,
    "project_rename": ProjectRenameInput,
    "project_remove": ProjectRemoveInput,
    "directory_list": DirectoryListInput,
    "file_read": FileReadInput,
    "text_search": TextSearchInput,
    "file_patch": FilePatchInput,
    "file_delete": FileDeleteInput,
    "execute_shell": ExecuteShellInput,
    "shell_status": ShellStatusInput,
    "shell_cancel": ShellCancelInput,
    "screen_list": ScreenListInput,
    "screenshot_capture": ScreenshotCaptureInput,
    "browser_open": BrowserOpenInput,
    # Code intelligence tools (language-neutral, all route to project_code wire tool)
    "code_intelligence_status": CodeStatusInput,
    "code_workspace_summary": CodeSummaryInput,
    "code_symbol_search": CodeSymbolSearchFacadeInput,
    "code_definition": CodeDefinitionFacadeInput,
    "code_references": CodeReferencesFacadeInput,
    "code_implementations": CodeImplementationsFacadeInput,
    "code_diagnostics": CodeDiagnosticsFacadeInput,
    "code_hover": CodeHoverFacadeInput,
    "code_context": CodeContextFacadeInput,
    "code_call_hierarchy": CodeCallHierarchyFacadeInput,
    "code_type_hierarchy": CodeTypeHierarchyFacadeInput,
    "code_impact": CodeImpactFacadeInput,
    "code_architecture": CodeArchitectureFacadeInput,
    "code_dependencies": CodeDependenciesFacadeInput,
    "code_related_tests": CodeRelatedTestsFacadeInput,
    "code_reindex": CodeReindexFacadeInput,
}


def facade_wire_request(  # noqa: PLR0911 - explicit bounded public-to-wire mapping.
    name: str, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Validate raw public fields before deterministic mapping; never infer approval."""
    request = FACADE_MODELS[name].model_validate(arguments)
    data = request.model_dump(mode="json", exclude_none=True)
    if name == "projects_list":
        return "project_read", {"operation": "list_projects", **data}
    if name in {"project_add", "project_rename", "project_remove"}:
        operation = {
            "project_add": "request_add_project",
            "project_rename": "rename_project",
            "project_remove": "remove_project",
        }[name]
        return "project_manage", {"operation": operation, **data}
    if name in {"directory_list", "file_read"}:
        operation = "list_directory" if name == "directory_list" else "read_file"
        if name == "file_read":
            data["end_line"] = data["start_line"] + data["max_lines"] - 1
        return "project_read", {"operation": operation, **data}
    if name == "text_search":
        return "project_read", {"operation": "search_text", **data}
    if name in {"file_patch", "file_delete"}:
        if name == "file_delete":
            path = data.pop("path")
            data["patch"] = f"*** Begin Patch\n*** Delete File: {path}\n*** End Patch\n"
            data["base_hashes"] = {path: data.pop("base_hash")}
        return "project_apply_patch", data
    if name == "execute_shell":
        data["command"] = {
            "kind": "script",
            "shell": data.pop("executor"),
            "script": data["command"],
        }
        cwd = data.pop("cwd")
        data[
            "external_working_directory" if re.match(r"^[A-Za-z]:/", cwd) else "working_directory"
        ] = cwd
        return "project_shell", {"action": "start", **data}
    if name in {"shell_status", "shell_cancel"}:
        return "project_shell", {"action": "poll" if name == "shell_status" else "cancel", **data}
    if name in {"screen_list", "screenshot_capture"}:
        return "device_screenshot", {
            "action": "list_displays" if name == "screen_list" else "capture",
            **data,
        }
    if name in _CODE_FACADE_TO_OPERATION:
        operation = _CODE_FACADE_TO_OPERATION[name]
        return "project_code", {"operation": operation, **data}
    return "device_desktop", {"action": "open_browser", **data}
