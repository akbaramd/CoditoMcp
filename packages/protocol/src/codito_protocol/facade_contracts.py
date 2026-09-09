"""Descriptors for the focused public tool surface; old names remain wire aliases."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .code import (
    CodeArchitectureResult,
    CodeCallHierarchyResult,
    CodeContextResult,
    CodeDefinitionResult,
    CodeDependenciesResult,
    CodeDiagnosticsResult,
    CodeHoverResult,
    CodeImpactResult,
    CodeImplementationsResult,
    CodeIntelligenceStatusResult,
    CodeReferencesResult,
    CodeReindexResult,
    CodeRelatedTestsResult,
    CodeSymbolSearchResult,
    CodeTypeHierarchyResult,
    CodeWorkspaceSummaryResult,
)
from .desktop_action import DeviceDesktopResult
from .errors import ToolError
from .manage import ProjectRegistrationRequestResult, RemoveProjectResult, RenameProjectResult
from .patch import ProjectApplyPatchResult
from .read import ListDirectoryResult, ListProjectsResult, ReadFileResult, SearchTextResult
from .screenshot import DeviceDisplaysResult, DisplaySelector
from .shell import ShellCancelResult, ShellPollResult, ShellStartResult
from .types import CoditoModel, Sha256


class FacadeToolResult[ResultT](CoditoModel):
    """Common result envelope with a tool-specific structured payload."""

    ok: bool
    text: str
    operation_id: str | None = None
    result: ResultT | None = None
    error: ToolError | None = None


class ScreenshotCaptureResult(CoditoModel):
    """Model-visible screenshot metadata; PNG bytes travel as MCP ImageContent."""

    mime_type: Literal["image/png"] = "image/png"
    display: DisplaySelector = "primary"
    width: int = Field(ge=1, le=2048)
    height: int = Field(ge=1, le=2048)
    captured_at: datetime
    sha256: Sha256


FACADE_OUTPUT_MODELS: dict[str, type[CoditoModel]] = {
    "projects_list": FacadeToolResult[ListProjectsResult],
    "project_add": FacadeToolResult[ProjectRegistrationRequestResult],
    "project_rename": FacadeToolResult[RenameProjectResult],
    "project_remove": FacadeToolResult[RemoveProjectResult],
    "directory_list": FacadeToolResult[ListDirectoryResult],
    "file_read": FacadeToolResult[ReadFileResult],
    "text_search": FacadeToolResult[SearchTextResult],
    "file_patch": FacadeToolResult[ProjectApplyPatchResult],
    "file_delete": FacadeToolResult[ProjectApplyPatchResult],
    "execute_shell": FacadeToolResult[ShellStartResult],
    "shell_status": FacadeToolResult[ShellPollResult],
    "shell_cancel": FacadeToolResult[ShellCancelResult],
    "screen_list": FacadeToolResult[DeviceDisplaysResult],
    "screenshot_capture": FacadeToolResult[ScreenshotCaptureResult],
    "browser_open": FacadeToolResult[DeviceDesktopResult],
    # Code intelligence
    "code_intelligence_status": FacadeToolResult[CodeIntelligenceStatusResult],
    "code_workspace_summary": FacadeToolResult[CodeWorkspaceSummaryResult],
    "code_symbol_search": FacadeToolResult[CodeSymbolSearchResult],
    "code_definition": FacadeToolResult[CodeDefinitionResult],
    "code_references": FacadeToolResult[CodeReferencesResult],
    "code_implementations": FacadeToolResult[CodeImplementationsResult],
    "code_diagnostics": FacadeToolResult[CodeDiagnosticsResult],
    "code_hover": FacadeToolResult[CodeHoverResult],
    "code_context": FacadeToolResult[CodeContextResult],
    "code_call_hierarchy": FacadeToolResult[CodeCallHierarchyResult],
    "code_type_hierarchy": FacadeToolResult[CodeTypeHierarchyResult],
    "code_impact": FacadeToolResult[CodeImpactResult],
    "code_architecture": FacadeToolResult[CodeArchitectureResult],
    "code_dependencies": FacadeToolResult[CodeDependenciesResult],
    "code_related_tests": FacadeToolResult[CodeRelatedTestsResult],
    "code_reindex": FacadeToolResult[CodeReindexResult],
}


def _contract(
    title: str,
    description: str,
    scopes: list[str],
    invoking: str,
    invoked: str,
    *,
    read: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "securitySchemes": [{"type": "oauth2", "scopes": scopes}],
        "annotations": {
            "readOnlyHint": read,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": open_world,
        },
        "invoking": invoking,
        "invoked": invoked,
    }


_READ = ["projects:read", "files:read"]
_WRITE = [*_READ, "files:write"]
_ANALYZE = [*_READ, "shell:execute"]
_MANAGE = ["projects:read", "projects:write"]
_SHELL = ["projects:read", "shell:execute"]
_SCREEN = ["projects:read", "screen:read"]
_SCOPE = (
    "Paths are relative to the origin project, or to an explicit requested scope_path. "
    "Each call uses that project's locally configured policy; scope_path never proves approval. "
)

FACADE_CONTRACTS: dict[str, dict[str, Any]] = {
    "projects_list": _contract(
        "List projects",
        "List this device's registered projects and their opaque IDs.",
        ["projects:read"],
        "Listing projects…",
        "Project list ready",
        read=True,
        idempotent=True,
    ),
    "project_add": _contract(
        "Add project",
        "Request a project title; the Windows user selects the local folder. "
        "Returns pending_local_selection, not a registered path.",
        _MANAGE,
        "Requesting project registration…",
        "Project registration response ready",
        idempotent=True,
    ),
    "project_rename": _contract(
        "Rename project",
        "Rename registered project metadata under local policy; files are unchanged.",
        _MANAGE,
        "Requesting project rename…",
        "Project rename response ready",
        idempotent=True,
    ),
    "project_remove": _contract(
        "Remove project",
        "Unregister a project under local policy; this does not delete its files.",
        _MANAGE,
        "Requesting project removal…",
        "Project removal response ready",
        destructive=True,
        idempotent=True,
    ),
    "directory_list": _contract(
        "List directory",
        _SCOPE + "List bounded file/directory metadata with glob and cursor pagination.",
        _READ,
        "Listing directory…",
        "Directory response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "file_read": _contract(
        "Read file",
        _SCOPE + "Read a bounded numbered text range with encoding, newlines and SHA-256. "
        "Use the returned hash as a mutation precondition; follow continuation for more lines.",
        _READ,
        "Reading file…",
        "File response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "text_search": _contract(
        "Search file text",
        _SCOPE + "Search bounded text/globs; returns matching paths, lines and continuation.",
        _READ,
        "Searching file text…",
        "Search response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "file_patch": _contract(
        "Patch files",
        _SCOPE + "Apply an exact anchored *** Begin Patch document with Add/Update/Delete/Move "
        "sections. Supply every touched path's base hash (null only for new files). "
        "No fuzzy matching; the full batch is preflighted and journaled. "
        "dry_run performs no writes.",
        _WRITE,
        "Checking and applying patch…",
        "Patch response ready",
        destructive=True,
        idempotent=True,
        open_world=True,
    ),
    "file_delete": _contract(
        "Delete file",
        _SCOPE + "Delete exactly one file against its current SHA-256. "
        "Uses the same journaled patch engine; cannot recursively remove directories. "
        "Read first if the current base_hash is unknown. dry_run performs no deletion.",
        _WRITE,
        "Checking file deletion…",
        "File deletion response ready",
        destructive=True,
        idempotent=True,
        open_world=True,
    ),
    "execute_shell": _contract(
        "Execute Windows command",
        "Run command as a noninteractive PowerShell or cmd script. "
        "Set executor, cwd and timeout_seconds directly. Uses the chosen project's local authority "
        "and installed Windows tools when policy permits; outside paths/cwd may require consent. "
        "Native cwd is not confinement. No PTY, elevation or automatic uncertain replay. "
        "If pending_approval, queued or running, continue with shell_status using the same job_id; "
        "do not resubmit command or wait for another user message just to poll.",
        _SHELL,
        "Running Windows command…",
        "Shell response ready",
        destructive=True,
        open_world=True,
    ),
    "shell_status": _contract(
        "Read shell status",
        "Poll the existing job's state and sequenced output. "
        "Reuse project_id/job_id and the returned sequence cursor; keep polling nonterminal jobs.",
        _SHELL,
        "Waiting for shell output…",
        "Shell status ready",
        read=True,
        idempotent=True,
    ),
    "shell_cancel": _contract(
        "Cancel shell job",
        "Request cancellation of an existing bounded process tree; "
        "use the original project_id/job_id. This is not a new command.",
        _SHELL,
        "Cancelling shell job…",
        "Cancellation response ready",
        destructive=True,
        idempotent=True,
    ),
    "screen_list": _contract(
        "List screens",
        "List opaque display IDs and topology metadata without capturing pixels.",
        ["screen:read"],
        "Listing Windows screens…",
        "Screen list ready",
        read=True,
    ),
    "screenshot_capture": _contract(
        "Capture screenshot",
        "Capture one selected Windows display as an actual PNG image. "
        "Use display from screen_list, or primary. Applies the origin project's screen policy; "
        "when consent is needed the local user can allow once or save same-monitor permission. "
        "Visible private windows can be included. Lock/UAC desktops are blocked. "
        "This does not authorize files, shell, mouse or keyboard control.",
        _SCREEN,
        "Capturing selected screen…",
        "Screenshot response ready",
        read=True,
        open_world=True,
    ),
    "browser_open": _contract(
        "Open browser",
        "Open a requested HTTP/HTTPS URL in the default browser or installed Firefox "
        "under the origin project's desktop policy. Existing browser cookies/profile may be used. "
        "Returns submitted, never proof the page loaded. No arbitrary apps, URI schemes, clicking "
        "or typing. Use screenshot_capture separately to inspect; never replay uncertain opens.",
        _SHELL,
        "Opening browser…",
        "Browser request response ready",
        destructive=True,
        open_world=True,
    ),
    # -----------------------------------------------------------------------
    # Code intelligence tools (language-neutral LSP/graph-backed)
    # -----------------------------------------------------------------------
    "code_intelligence_status": _contract(
        "Code intelligence status",
        "Report the active workspace state, provider, capabilities, and freshness for a registered "
        "project. Does not download or start a language server on its own — only reflects what is "
        "already configured and running. Check capabilities before calling semantic tools.",
        _READ,
        "Checking code intelligence…",
        "Status ready",
        read=True,
        idempotent=True,
    ),
    "code_workspace_summary": _contract(
        "Workspace summary",
        "Return languages, manifest files, framework hints (Next.js/React are JS/TS hints, "
        "not additional languages), detected build roots, and structural project layout. "
        "Based on file analysis of the current snapshot; results include provenance.",
        _READ,
        "Analysing workspace…",
        "Summary ready",
        read=True,
        idempotent=True,
    ),
    "code_symbol_search": _contract(
        "Search symbols",
        "Search workspace or document symbols by name. Returns bounded matches with kind, "
        "location, and source precision. Scope=document requires a file path. "
        "Results include snapshot_id and precision; truncation is explicit.",
        _ANALYZE,
        "Searching symbols…",
        "Symbol search ready",
        read=True,
        idempotent=True,
    ),
    "code_definition": _contract(
        "Go to definition",
        "Resolve the semantic definition of the symbol at a 1-based line/character position. "
        "Returns all definition locations with precision. Exact (LSP) vs structural is labelled. "
        "External library definitions are returned as external_unavailable.",
        _ANALYZE,
        "Finding definition…",
        "Definition ready",
        read=True,
        idempotent=True,
    ),
    "code_references": _contract(
        "Find references",
        "Find semantic references to the symbol at a 1-based position. "
        "Structural or heuristic candidates are never passed off as exact. "
        "include_declaration controls whether the declaration itself is included. "
        "Results include precision, truncation flag, and total match count.",
        _ANALYZE,
        "Finding references…",
        "References ready",
        read=True,
        idempotent=True,
    ),
    "code_implementations": _contract(
        "Find implementations",
        "Find concrete implementations of an interface or abstract symbol at a 1-based position. "
        "Returns unavailable with an explanation when the language server does not support it. "
        "Precision is always reported; never conflates structural with semantic.",
        _ANALYZE,
        "Finding implementations…",
        "Implementations ready",
        read=True,
        idempotent=True,
    ),
    "code_diagnostics": _contract(
        "Get diagnostics",
        "Return compiler/language-server diagnostics for a project-relative file. "
        "State can be ready, stale, partial, pending, or unavailable — missing diagnostics "
        "NEVER mean a clean build. severity_min filters by error/warning/information/hint. "
        "Snapshot_id and captured_at show freshness.",
        _ANALYZE,
        "Reading diagnostics…",
        "Diagnostics ready",
        read=True,
        idempotent=True,
    ),
    "code_hover": _contract(
        "Hover information",
        "Return hover signature, type, and documentation at a 1-based position. "
        "Preserves markdown or plaintext format without reinventing structure from free text. "
        "Returns null content (not an error) when no hover information is available.",
        _ANALYZE,
        "Fetching hover…",
        "Hover ready",
        read=True,
        idempotent=True,
    ),
    "code_context": _contract(
        "Rich code context",
        "Aggregate definition, hover, references, nearby source lines, and diagnostics from "
        "ONE coherent snapshot at a 1-based position. Each section reports its own "
        "availability and precision. Never aggregates across mismatched generations.",
        _ANALYZE,
        "Building context…",
        "Context ready",
        read=True,
        idempotent=True,
    ),
    "code_call_hierarchy": _contract(
        "Call hierarchy",
        "Prepare call hierarchy at a 1-based position and traverse incoming/outgoing calls. "
        "Bounded by max_depth and max_nodes. Precision is exact (LSP) or structural (graph). "
        "Returns unavailable when the server does not support call hierarchy.",
        _ANALYZE,
        "Building call hierarchy…",
        "Call hierarchy ready",
        read=True,
        idempotent=True,
    ),
    "code_type_hierarchy": _contract(
        "Type hierarchy",
        "Prepare type hierarchy at a 1-based position and traverse supertypes/subtypes. "
        "Bounded by max_depth and max_nodes. Precision is exact (LSP) or structural (graph). "
        "Returns unavailable when the server does not support type hierarchy.",
        _ANALYZE,
        "Building type hierarchy…",
        "Type hierarchy ready",
        read=True,
        idempotent=True,
    ),
    "code_impact": _contract(
        "Code impact",
        "Return bounded graph dependents for a path or symbol with evidence and precision. "
        "Dynamic and reflection effects are never claimed. Graph provider must be configured; "
        "returns precision=unavailable without one. Never exposes all-project graph results.",
        _ANALYZE,
        "Analysing impact…",
        "Impact ready",
        read=True,
        idempotent=True,
    ),
    "code_architecture": _contract(
        "Architecture overview",
        "Return structural package/dependency graph from manifests and graph analysis. "
        "Separates structural facts from inferred layers. Graph provider optional; "
        "manifest-only analysis is available without one.",
        _ANALYZE,
        "Analysing architecture…",
        "Architecture ready",
        read=True,
        idempotent=True,
    ),
    "code_dependencies": _contract(
        "Project dependencies",
        "Return manifest-backed or graph-backed dependencies and direction. "
        "Not arbitrary SQL or Cypher; only the registered project scope is queried. "
        "direction=direct|transitive|all; precision reflects manifest vs graph source.",
        _ANALYZE,
        "Listing dependencies…",
        "Dependencies ready",
        read=True,
        idempotent=True,
    ),
    "code_related_tests": _contract(
        "Related tests",
        "Return test files/functions related to a source file. Relationship is exact "
        "(from graph or framework conventions) or explicitly labelled heuristic_name / "
        "heuristic_path. Never presents candidates as guaranteed coverage.",
        _ANALYZE,
        "Finding related tests…",
        "Related tests ready",
        read=True,
        idempotent=True,
    ),
    "code_reindex": _contract(
        "Reindex workspace",
        "Explicitly trigger incremental or full refresh of the code intelligence index "
        "for a registered project. Does not acknowledge completion until indexing finishes. "
        "scope=incremental updates changed files; scope=full rebuilds from scratch. "
        "Reports timed_out=true if timeout_seconds is reached before completion.",
        _ANALYZE,
        "Reindexing workspace…",
        "Reindex complete",
        read=True,
        idempotent=False,
    ),
}
