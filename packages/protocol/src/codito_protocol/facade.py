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
from .frontend import (
    FRONTEND_ROUTE_PATTERN,
    MAX_FRONTEND_URL_CHARS,
    FrontendActInput,
    FrontendAction,
    FrontendKey,
    FrontendSessionStartInput,
    FrontendTarget,
    FrontendViewport,
    validate_frontend_route,
)
from .patch import ProjectApplyPatchInput
from .screenshot import DisplaySelector
from .shell import ScriptCommand
from .types import CoditoModel, OpaqueId, ProjectGlob, RelativePath, Sha256

Purpose = Annotated[str, Field(min_length=1, max_length=1000)]
Cursor = Annotated[str | None, Field(max_length=512)]
_FIRST_PRINTABLE = 32
_PROJECT_ID = "Opaque project ID returned by projects_list; never send a local path."
_PURPOSE = "Concise user-facing reason shown in Windows approval and local activity history."
_IDEMPOTENCY = (
    "Caller-generated stable unique key for this logical mutation or command. "
    "Reuse only when retrying that exact same request."
)
_CURSOR = "Opaque continuation returned by the same tool and unchanged query; do not edit it."


class ProjectsListInput(CoditoModel):
    cursor: Cursor = Field(default=None, description=_CURSOR)
    limit: int = Field(default=50, ge=1, le=100, description="Maximum projects to return.")


class ProjectAddInput(CoditoModel):
    title: str = Field(
        min_length=1,
        max_length=120,
        description="Human-readable project title; Windows separately asks the user for a folder.",
    )
    idempotency_key: OpaqueId = Field(description=_IDEMPOTENCY)


class ProjectRenameInput(ProjectAddInput):
    project_id: OpaqueId = Field(description=_PROJECT_ID)


class ProjectRemoveInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    idempotency_key: OpaqueId = Field(description=_IDEMPOTENCY)


class ProjectTarget(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    scope_path: str | None = Field(
        default=None,
        description=(
            "Optional requested absolute Windows directory outside the project, e.g. C:/Temp. "
            "Local project policy and Windows approval still apply; not a trust assertion."
        ),
    )
    purpose: Purpose = Field(description=_PURPOSE)

    @field_validator("scope_path")
    @classmethod
    def normalize_scope(cls, value: str | None) -> str | None:
        return normalize_read_scope(value) if value is not None else None


class FileReadInput(ProjectTarget):
    path: RelativePath = Field(
        description="Exact file path relative to the project root or requested scope_path."
    )
    start_line: int = Field(
        default=1, ge=1, le=1_000_000, description="One-based first line to return."
    )
    max_lines: int = Field(
        default=200, ge=1, le=2000, description="Maximum numbered lines to return in this page."
    )
    max_bytes: int = Field(
        default=65536,
        ge=256,
        le=262144,
        description="Maximum file-content bytes returned in this page.",
    )
    continuation: Cursor = Field(default=None, description=_CURSOR)

    @model_validator(mode="after")
    def valid_file(self) -> FileReadInput:
        if not self.path:
            raise ValueError("A nonempty file path is required")
        return self


class DirectoryListInput(ProjectTarget):
    path: RelativePath = Field(
        default="", description="Directory relative to project root or scope_path; empty is root."
    )
    glob: ProjectGlob = Field(
        default="*", description="Filename glob applied below path, for example **/*.py."
    )
    recursive: bool = Field(
        default=False, description="Whether to recursively traverse descendants below path."
    )
    cursor: Cursor = Field(default=None, description=_CURSOR)
    limit: int = Field(default=100, ge=1, le=500, description="Maximum entries to return.")


class TextSearchInput(ProjectTarget):
    query: str = Field(
        min_length=1,
        max_length=4096,
        description="Literal text to find, or a bounded regular expression when regex=true.",
    )
    path: RelativePath = Field(
        default="",
        description="File or directory relative to project root or scope_path; empty is root.",
    )
    glob: ProjectGlob = Field(
        default="**/*", description="File glob limiting which files are searched."
    )
    case_sensitive: bool = Field(default=False, description="Match letter case exactly when true.")
    regex: bool = Field(default=False, description="Interpret query as a regular expression.")
    max_results: int = Field(
        default=100, ge=1, le=500, description="Maximum matching locations to return."
    )
    max_bytes_per_match: int = Field(
        default=2048,
        ge=128,
        le=16384,
        description="Maximum UTF-8 preview bytes returned for each match.",
    )
    continuation: Cursor = Field(default=None, description=_CURSOR)


class FilePatchInput(ProjectTarget):
    patch: str = Field(min_length=35, description="Exact anchored *** Begin Patch document.")
    base_hashes: dict[RelativePath, Sha256 | None] = Field(
        min_length=1,
        max_length=256,
        description=(
            "Exact precondition for every source/destination path: current SHA-256 from file_read, "
            "or null only when an added destination must not exist."
        ),
    )
    idempotency_key: OpaqueId = Field(description=_IDEMPOTENCY)
    dry_run: bool = Field(
        default=False, description="Preflight and report the patch without changing files."
    )

    @model_validator(mode="after")
    def valid_patch(self) -> FilePatchInput:
        # Reuse exact grammar, byte bounds, and complete preconditions; never
        # introduce a weaker public parser than the existing wire contract.
        ProjectApplyPatchInput.model_validate(self.model_dump(exclude={"scope_path", "purpose"}))
        return self


class FileDeleteInput(ProjectTarget):
    path: RelativePath = Field(
        description="Exact single file to delete, relative to the target root."
    )
    base_hash: Sha256 = Field(description="Current SHA-256 returned by file_read.")
    idempotency_key: OpaqueId = Field(description=_IDEMPOTENCY)
    dry_run: bool = Field(
        default=False, description="Validate the deletion without changing the file."
    )

    @field_validator("path")
    @classmethod
    def nonempty_patch_path(cls, value: str) -> str:
        if not value or any(ord(character) < _FIRST_PRINTABLE for character in value):
            raise ValueError("A nonempty file path without control characters is required")
        return value


class ExecuteShellInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    command: str = Field(
        min_length=1,
        description=(
            "Exact noninteractive command/script to execute. Do not put file reads, searches, "
            "edits, or deletes here when a dedicated Codito tool can perform them."
        ),
    )
    executor: Literal["powershell", "cmd"] = Field(
        default="powershell", description="Windows command interpreter used for command."
    )
    purpose: Purpose = Field(description=_PURPOSE)
    idempotency_key: OpaqueId = Field(description=_IDEMPOTENCY)
    cwd: str = Field(
        default="",
        description="Project-relative cwd (empty=root), or absolute requested Windows directory.",
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=1800,
        description="Maximum runtime before the process tree is stopped.",
    )
    output_limit_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1024,
        le=10 * 1024 * 1024,
        description="Maximum combined stdout/stderr retained for this job.",
    )
    approval_timeout_seconds: int = Field(
        default=180,
        ge=15,
        le=300,
        description="Maximum time to wait for required local Windows approval.",
    )
    start_wait_milliseconds: int = Field(
        default=30000,
        ge=0,
        le=30000,
        description="Initial server wait for completion/output before returning a job state.",
    )
    requested_external_paths: list[str] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Absolute Windows paths the command is expected to access outside cwd/project, "
            "shown to local policy; this declaration never grants access."
        ),
    )

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
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    job_id: OpaqueId = Field(description="Job ID returned by execute_shell.")
    sequence_cursor: int = Field(
        default=0, ge=0, description="Next output sequence cursor returned by shell_status."
    )
    wait_milliseconds: int = Field(
        default=30000,
        ge=0,
        le=30000,
        description="Maximum long-poll wait for new output or a terminal state.",
    )


class ShellCancelInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    job_id: OpaqueId = Field(description="Running job ID returned by execute_shell.")
    reason: str = Field(
        default="cancelled by caller",
        min_length=1,
        max_length=500,
        description="Short audit reason for cancelling this job.",
    )


class ScreenListInput(CoditoModel):
    purpose: Purpose = Field(
        default="List available Windows screens without capturing pixels",
        description=_PURPOSE,
    )


class ScreenshotCaptureInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    display: DisplaySelector = Field(
        default="primary", description="primary or an opaque display ID returned by screen_list."
    )
    max_dimension: int = Field(
        default=1600,
        ge=640,
        le=2048,
        description="Maximum output width or height; aspect ratio is preserved.",
    )


class BrowserOpenInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    url: str = Field(description="HTTP/HTTPS destination; no credentials or custom schemes.")
    purpose: Purpose = Field(description=_PURPOSE)
    browser: Literal["default", "firefox"] = Field(
        default="default", description="Use the Windows default browser or installed Firefox."
    )

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return DeviceDesktopInput(url=value, purpose="Validate browser destination").url


# ---------------------------------------------------------------------------
# Managed frontend public inputs (all route to project_frontend)
# ---------------------------------------------------------------------------


class FrontendSessionStartFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    route: str = Field(
        default="/",
        max_length=MAX_FRONTEND_URL_CHARS,
        json_schema_extra={"pattern": FRONTEND_ROUTE_PATTERN},
        description=(
            "App-relative path beginning with /; origins, absolute URLs, queries, and fragments "
            "are forbidden. Navigate to parameterized state through the UI."
        ),
    )
    viewport: FrontendViewport = Field(
        default_factory=FrontendViewport,
        description="Managed Chromium viewport used for screenshots and coordinate targeting.",
    )
    ready_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Maximum wait for the configured or detected local development server.",
    )
    idempotency_key: OpaqueId = Field(description=_IDEMPOTENCY)

    _valid_route = field_validator("route")(validate_frontend_route)

    @model_validator(mode="after")
    def valid_start(self) -> FrontendSessionStartFacadeInput:
        FrontendSessionStartInput.model_validate(
            {"operation": "session_start", **self.model_dump()}
        )
        return self


class FrontendSnapshotFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    session_id: OpaqueId = Field(
        description="Frontend session ID returned by frontend_session_start."
    )
    purpose: Purpose = Field(description=_PURPOSE)
    max_elements: int = Field(
        default=500,
        ge=1,
        le=500,
        description="Maximum semantic elements returned with the viewport PNG.",
    )


class FrontendInspectFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    session_id: OpaqueId = Field(
        description="Frontend session ID returned by frontend_session_start."
    )
    purpose: Purpose = Field(description=_PURPOSE)
    snapshot_id: OpaqueId = Field(description="Snapshot ID that minted this target registry.")
    element_id: str | None = Field(
        default=None,
        pattern=r"^e[1-9][0-9]{0,7}$",
        description="Semantic element ID from that snapshot; use this or x/y, never both.",
    )
    x: int | None = Field(
        default=None,
        ge=0,
        le=2047,
        description="Viewport x coordinate from that snapshot; requires y and excludes element_id.",
    )
    y: int | None = Field(
        default=None,
        ge=0,
        le=2047,
        description="Viewport y coordinate from that snapshot; requires x and excludes element_id.",
    )

    @model_validator(mode="after")
    def valid_target(self) -> FrontendInspectFacadeInput:
        FrontendTarget.model_validate(
            self.model_dump(include={"snapshot_id", "element_id", "x", "y"})
        )
        return self


class FrontendActFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    session_id: OpaqueId = Field(
        description="Frontend session ID returned by frontend_session_start."
    )
    purpose: Purpose = Field(description=_PURPOSE)
    snapshot_id: OpaqueId = Field(description="Snapshot ID that minted element_id; stale IDs fail.")
    element_id: str = Field(
        pattern=r"^e[1-9][0-9]{0,7}$",
        description="Semantic element ID returned by the identified frontend snapshot.",
    )
    action: FrontendAction = Field(
        description="Bounded interaction: click, hover, focus, fill, press, scroll, or select."
    )
    text: str | None = Field(
        default=None,
        max_length=16_384,
        description="Text required only for fill; password targets are refused by the agent.",
    )
    key: FrontendKey | None = Field(
        default=None,
        description="Allowlisted key required only for press; shortcuts are forbidden.",
    )
    option: str | None = Field(
        default=None, max_length=1_024, description="Visible/value option required only for select."
    )
    delta_x: int | None = Field(
        default=None, ge=-2_048, le=2_048, description="Horizontal delta used only for scroll."
    )
    delta_y: int | None = Field(
        default=None, ge=-2_048, le=2_048, description="Vertical delta used only for scroll."
    )
    idempotency_key: OpaqueId = Field(description=_IDEMPOTENCY)

    @model_validator(mode="after")
    def valid_action(self) -> FrontendActFacadeInput:
        FrontendActInput.model_validate({"operation": "act", **self.model_dump()})
        return self


class FrontendSourceFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    session_id: OpaqueId = Field(
        description="Frontend session ID returned by frontend_session_start."
    )
    purpose: Purpose = Field(description=_PURPOSE)
    snapshot_id: OpaqueId = Field(description="Snapshot ID that minted element_id; stale IDs fail.")
    element_id: str = Field(
        pattern=r"^e[1-9][0-9]{0,7}$",
        description="Semantic element ID whose project-relative source locations are requested.",
    )


class FrontendSessionStopFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    session_id: OpaqueId = Field(
        description="Frontend session ID returned by frontend_session_start."
    )
    purpose: Purpose = Field(description=_PURPOSE)
    reason: str = Field(
        default="Frontend review complete",
        min_length=1,
        max_length=500,
        description="Short audit reason for closing the managed browser session.",
    )


# ---------------------------------------------------------------------------
# Code intelligence public inputs (language-neutral; 1-based lines, Unicode scalars)
# ---------------------------------------------------------------------------


class CodeStatusInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(default="Query code intelligence status", description=_PURPOSE)


class CodeSummaryInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(default="Summarise workspace", description=_PURPOSE)


class CodeSymbolSearchFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    query: str = Field(
        min_length=1, max_length=512, description="Symbol name or partial symbol name to find."
    )
    scope: Literal["workspace", "document"] = Field(
        default="workspace", description="Search the whole project or one document."
    )
    path: RelativePath | None = Field(
        default=None, description="Required project-relative source file when scope=document."
    )
    kinds: list[SymbolKind] = Field(
        default_factory=list,
        max_length=27,
        description="Optional symbol-kind filter; empty allows every supported kind.",
    )
    max_results: int = Field(
        default=50, ge=1, le=500, description="Maximum symbol matches to return."
    )


class CodeDefinitionFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file containing the symbol.")
    line: int = Field(ge=1, le=10_000_000, description="One-based symbol line.")
    character: int = Field(ge=1, le=100_000, description="One-based Unicode character position.")


class CodeReferencesFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file containing the symbol.")
    line: int = Field(ge=1, le=10_000_000, description="One-based symbol line.")
    character: int = Field(ge=1, le=100_000, description="One-based Unicode character position.")
    include_declaration: bool = Field(
        default=True, description="Include the symbol declaration in returned locations."
    )
    max_results: int = Field(
        default=100, ge=1, le=1000, description="Maximum reference locations to return."
    )


class CodeImplementationsFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file containing the symbol.")
    line: int = Field(ge=1, le=10_000_000, description="One-based symbol line.")
    character: int = Field(ge=1, le=100_000, description="One-based Unicode character position.")
    max_results: int = Field(
        default=100, ge=1, le=500, description="Maximum implementation locations to return."
    )


class CodeDiagnosticsFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file to diagnose.")
    severity_min: DiagnosticSeverity = Field(
        default=DiagnosticSeverity.HINT,
        description="Lowest diagnostic severity to include.",
    )
    max_results: int = Field(
        default=200, ge=1, le=1000, description="Maximum diagnostics to return."
    )


class CodeHoverFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file containing the symbol.")
    line: int = Field(ge=1, le=10_000_000, description="One-based symbol line.")
    character: int = Field(ge=1, le=100_000, description="One-based Unicode character position.")


class CodeContextFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file containing the symbol.")
    line: int = Field(ge=1, le=10_000_000, description="One-based symbol line.")
    character: int = Field(ge=1, le=100_000, description="One-based Unicode character position.")
    source_context_lines: int = Field(
        default=5, ge=0, le=50, description="Nearby source lines to include on each side."
    )
    include_references: bool = Field(
        default=True, description="Include bounded symbol references in the aggregate."
    )
    include_diagnostics: bool = Field(
        default=True, description="Include current diagnostics in the aggregate."
    )


class CodeCallHierarchyFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file containing the callable.")
    line: int = Field(ge=1, le=10_000_000, description="One-based callable line.")
    character: int = Field(ge=1, le=100_000, description="One-based Unicode character position.")
    direction: HierarchyDirection = Field(
        default=HierarchyDirection.BOTH, description="Incoming, outgoing, or both call directions."
    )
    max_depth: int = Field(default=3, ge=1, le=10, description="Maximum traversal depth.")
    max_nodes: int = Field(default=50, ge=1, le=200, description="Maximum graph nodes returned.")


class CodeTypeHierarchyFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file containing the type.")
    line: int = Field(ge=1, le=10_000_000, description="One-based type line.")
    character: int = Field(ge=1, le=100_000, description="One-based Unicode character position.")
    direction: TypeHierarchyDirection = Field(
        default=TypeHierarchyDirection.BOTH,
        description="Supertypes, subtypes, or both directions.",
    )
    max_depth: int = Field(default=3, ge=1, le=10, description="Maximum traversal depth.")
    max_nodes: int = Field(default=50, ge=1, le=200, description="Maximum graph nodes returned.")


class CodeImpactFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative file whose dependents are requested.")
    line: int | None = Field(
        default=None, ge=1, le=10_000_000, description="Optional one-based symbol line."
    )
    character: int | None = Field(
        default=None, ge=1, le=100_000, description="Optional one-based symbol character."
    )
    max_nodes: int = Field(default=50, ge=1, le=500, description="Maximum impact nodes returned.")


class CodeArchitectureFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    max_packages: int = Field(
        default=100, ge=1, le=500, description="Maximum package/layer nodes returned."
    )


class CodeDependenciesFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath | None = Field(
        default=None, description="Optional project-relative manifest, package, or source path."
    )
    direction: Literal["all", "direct", "transitive"] = Field(
        default="direct", description="Return direct, transitive, or all known dependencies."
    )
    max_results: int = Field(
        default=100, ge=1, le=500, description="Maximum dependency records returned."
    )


class CodeRelatedTestsFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(description=_PURPOSE)
    path: RelativePath = Field(description="Project-relative source file to find tests for.")
    max_results: int = Field(
        default=50, ge=1, le=200, description="Maximum related test candidates returned."
    )


class CodeReindexFacadeInput(CoditoModel):
    project_id: OpaqueId = Field(description=_PROJECT_ID)
    purpose: Purpose = Field(default="Reindex project", description=_PURPOSE)
    scope: ReindexScope = Field(
        default=ReindexScope.INCREMENTAL,
        description="Incremental refresh or explicit full index rebuild.",
    )
    timeout_seconds: int = Field(
        default=30, ge=5, le=30, description="Maximum wait for the indexing operation."
    )


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

_FRONTEND_FACADE_TO_OPERATION: dict[str, str] = {
    "frontend_session_start": "session_start",
    "frontend_snapshot": "snapshot",
    "frontend_inspect": "inspect",
    "frontend_act": "act",
    "frontend_source": "source",
    "frontend_session_stop": "session_stop",
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
    # Managed local Chromium tools (all route to project_frontend wire tool)
    "frontend_session_start": FrontendSessionStartFacadeInput,
    "frontend_snapshot": FrontendSnapshotFacadeInput,
    "frontend_inspect": FrontendInspectFacadeInput,
    "frontend_act": FrontendActFacadeInput,
    "frontend_source": FrontendSourceFacadeInput,
    "frontend_session_stop": FrontendSessionStopFacadeInput,
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
    if name in _FRONTEND_FACADE_TO_OPERATION:
        return "project_frontend", {
            "operation": _FRONTEND_FACADE_TO_OPERATION[name],
            **data,
        }
    if name in _CODE_FACADE_TO_OPERATION:
        operation = _CODE_FACADE_TO_OPERATION[name]
        return "project_code", {"operation": operation, **data}
    return "device_desktop", {"action": "open_browser", **data}
