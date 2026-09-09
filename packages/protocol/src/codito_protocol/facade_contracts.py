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
from .frontend import (
    FrontendActResult,
    FrontendInspectResult,
    FrontendSessionStartResult,
    FrontendSessionStopResult,
    FrontendSnapshotMetadata,
    FrontendSourceResult,
)
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
    "frontend_session_start": FacadeToolResult[FrontendSessionStartResult],
    "frontend_snapshot": FacadeToolResult[FrontendSnapshotMetadata],
    "frontend_inspect": FacadeToolResult[FrontendInspectResult],
    "frontend_act": FacadeToolResult[FrontendActResult],
    "frontend_source": FacadeToolResult[FrontendSourceResult],
    "frontend_session_stop": FacadeToolResult[FrontendSessionStopResult],
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
_FRONTEND_READ = ["projects:read", "frontend:read"]
_FRONTEND_INTERACT = ["projects:read", "frontend:interact"]
_SCOPE = (
    "Paths are relative to the origin project, or to an explicit requested scope_path. "
    "Each call uses that project's locally configured policy; scope_path never proves approval. "
)

MCP_SERVER_INSTRUCTIONS = """Codito gives access to one Windows device through focused tools.

Tool-selection rules (follow in this order):
1. Use projects_list when a project_id is unknown. Never invent an ID or local absolute path.
2. Prefer dedicated read tools over execute_shell: directory_list for names/metadata, file_read for
   exact file contents and hashes, and text_search for literal/regex discovery. Parallelize
   independent read-only calls when possible, then follow continuation cursors until sufficient.
3. Prefer semantic code_* tools over text_search or execute_shell for symbols, definitions,
   references, diagnostics, hierarchies, dependencies, architecture, impact, and related tests.
   Use file_read after discovery when exact source text or a mutation hash is required.
4. Use file_patch for all text-file additions, updates, moves, and multi-file deletions. Use
   file_delete for one exact deletion. Do not create/edit/delete files through execute_shell when
   these dedicated tools can perform the request. Read current files first and supply exact hashes.
5. Use execute_shell only for genuine command execution such as builds, tests, formatters, Git,
   package managers, Docker, SSH, or programs without a dedicated Codito tool. Do not use it merely
   to read, list, search, concatenate, edit, or delete files. Poll nonterminal jobs with
   shell_status; never resubmit an uncertain command. Use shell_cancel only to stop an existing job.
6. For development UI work, use frontend_session_start -> frontend_snapshot, then inspect or act
   only with IDs from that exact snapshot. Take a fresh snapshot after every action, use
   frontend_source to resolve project-relative source, and stop the session when finished.
   browser_open and screenshot_capture operate on the user's desktop and are not substitutes for
   the managed frontend browser, semantic tree, DOM/CSS inspection, or source mapping.
7. Use screen_list before screenshot_capture when the requested desktop display is ambiguous.
   Use browser_open only to open an HTTP/HTTPS URL in the user's ordinary browser.
8. Every call is independently authorized by OAuth and the selected project's local policy.
   Inputs request actions but never assert approval, trust, or elevation.

Examples:
- Inspect several known files: call file_read for each file (in parallel), not execute_shell with
  Get-Content/type. Locate unknown text with text_search, then read the relevant ranges.
- Inspect a directory tree: use directory_list with glob/recursive, not Get-ChildItem/dir.
- Modify source: file_read -> file_patch -> file_read or code_diagnostics to verify.
- Build, test, run Docker, or connect with SSH: execute_shell -> shell_status until terminal.
"""

FACADE_CONTRACTS: dict[str, dict[str, Any]] = {
    "projects_list": _contract(
        "List projects",
        "Use when project IDs or availability are unknown. Lists registered projects, local policy "
        "mode, online state, and opaque IDs required by project-scoped tools; never returns paths.",
        ["projects:read"],
        "Listing projects…",
        "Project list ready",
        read=True,
        idempotent=True,
    ),
    "project_add": _contract(
        "Add project",
        "Use only when the user asks to register a new local project. Requests a title, then the "
        "Windows user selects the folder; returns pending_local_selection, never a local path.",
        _MANAGE,
        "Requesting project registration…",
        "Project registration response ready",
        idempotent=True,
    ),
    "project_rename": _contract(
        "Rename project",
        "Use only to change a registered project's display title. Files, folders, IDs, and local "
        "paths are unchanged; do not use file_patch or execute_shell for this metadata action.",
        _MANAGE,
        "Requesting project rename…",
        "Project rename response ready",
        idempotent=True,
    ),
    "project_remove": _contract(
        "Remove project",
        "Use only when the user asks to unregister a project from Codito. This never deletes the "
        "folder or files; use file_delete only for an explicitly requested file deletion.",
        _MANAGE,
        "Requesting project removal…",
        "Project removal response ready",
        destructive=True,
        idempotent=True,
    ),
    "directory_list": _contract(
        "List directory",
        _SCOPE + "Use to discover file/directory names, types, sizes, and hashes with glob, "
        "recursion, and pagination. Prefer this over execute_shell dir/Get-ChildItem/find.",
        _READ,
        "Listing directory…",
        "Directory response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "file_read": _contract(
        "Read file",
        _SCOPE + "Use whenever exact text from a known file is needed. Returns numbered content, "
        "encoding, newline style, size, SHA-256, truncation, and continuation; prefer this over "
        "execute_shell Get-Content/type/cat and use the hash before mutation.",
        _READ,
        "Reading file…",
        "File response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "text_search": _contract(
        "Search file text",
        _SCOPE + "Use to locate literal or regex text across a file or filtered tree when exact "
        "locations are unknown. Returns paths, line/column, previews, and continuation; prefer "
        "this over execute_shell Select-String/findstr/grep, then use file_read for surrounding "
        "source.",
        _READ,
        "Searching file text…",
        "Search response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "file_patch": _contract(
        "Patch files",
        _SCOPE + "Use for every text-file add, update, move, or multi-file delete instead of shell "
        "redirection or scripts. Apply an exact anchored *** Begin Patch document with "
        "Add/Update/Delete/Move "
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
        _SCOPE + "Use for one explicit file deletion instead of del/Remove-Item. Delete exactly "
        "one file against its current SHA-256. "
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
        "Use only for genuine process execution: builds, tests, formatters, Git, package managers, "
        "Docker, SSH, or programs without a dedicated Codito tool. Do NOT use for file reading, "
        "listing, searching, editing, or deletion when file_read, directory_list, text_search, "
        "file_patch, or file_delete can do it. Run as a noninteractive PowerShell or cmd script. "
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
        "Use only after execute_shell returns a nonterminal job. Poll that existing job's state "
        "and sequenced stdout/stderr; never start or resubmit a command here. "
        "Reuse project_id/job_id and the returned sequence cursor; keep polling nonterminal jobs.",
        _SHELL,
        "Waiting for shell output…",
        "Shell status ready",
        read=True,
        idempotent=True,
    ),
    "shell_cancel": _contract(
        "Cancel shell job",
        "Use only to stop a nonterminal job previously returned by execute_shell. Request "
        "cancellation of its bounded process tree; "
        "use the original project_id/job_id. This is not a new command.",
        _SHELL,
        "Cancelling shell job…",
        "Cancellation response ready",
        destructive=True,
        idempotent=True,
    ),
    "screen_list": _contract(
        "List screens",
        "Use before screenshot_capture when a display is not already known or the user asks what "
        "screens exist. Lists opaque display IDs and topology metadata without capturing pixels.",
        ["screen:read"],
        "Listing Windows screens…",
        "Screen list ready",
        read=True,
    ),
    "screenshot_capture": _contract(
        "Capture screenshot",
        "Use when the user asks to see or inspect the current Windows screen. Capture one selected "
        "display as actual PNG ImageContent; this is not a file read and execute_shell cannot "
        "substitute for it. "
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
        "Use only when the user asks to open an HTTP/HTTPS page on the Windows device. Open it in "
        "the default browser or installed Firefox "
        "under the origin project's desktop policy. Existing browser cookies/profile may be used. "
        "Returns submitted, never proof the page loaded. No arbitrary apps, URI schemes, clicking "
        "or typing. Use screenshot_capture separately to inspect; never replay uncertain opens.",
        _SHELL,
        "Opening browser…",
        "Browser request response ready",
        destructive=True,
        open_world=True,
    ),
    "frontend_session_start": _contract(
        "Start frontend session",
        "Use to open a project route in an agent-owned Chromium session on the Windows device. "
        "The agent reuses or starts the locally configured development server, waits for HTTP "
        "readiness, and returns a session ID. Pass only an app-relative route; browser profiles, "
        "commands, ports, and filesystem roots come from trusted local project configuration.",
        [*_FRONTEND_INTERACT, "shell:execute"],
        "Starting managed frontend…",
        "Frontend session ready",
        destructive=True,
        open_world=True,
    ),
    "frontend_snapshot": _contract(
        "Capture frontend snapshot",
        "Use after frontend_session_start and after every interaction. Returns a viewport PNG as "
        "MCP ImageContent plus bounded semantic elements, layout boxes, console messages, network "
        "issues, and a fresh snapshot ID. Element IDs are valid only with that snapshot ID.",
        _FRONTEND_READ,
        "Capturing frontend snapshot…",
        "Frontend snapshot ready",
        read=True,
        open_world=True,
    ),
    "frontend_inspect": _contract(
        "Inspect frontend element",
        "Use to inspect one element from a specific frontend_snapshot by its semantic element ID, "
        "or resolve one viewport x/y point from that same snapshot. Returns bounded DOM identity, "
        "box geometry, selected computed styles, matched CSS rules, and accessibility semantics. "
        "Raw CSS selectors and JavaScript are never accepted.",
        _FRONTEND_READ,
        "Inspecting frontend element…",
        "Frontend inspection ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "frontend_act": _contract(
        "Interact with frontend",
        "Use to click, hover, focus, fill, press an allowlisted key, scroll, or select on one "
        "snapshot-bound semantic element. Action-specific fields are validated; stale element IDs, "
        "password or credential-like fills, file uploads, arbitrary selectors, scripts, and "
        "modifier shortcuts are refused. A completed action invalidates the snapshot, so capture "
        "a new one before reuse.",
        _FRONTEND_INTERACT,
        "Applying frontend interaction…",
        "Frontend interaction complete",
        destructive=True,
        open_world=True,
    ),
    "frontend_source": _contract(
        "Resolve frontend source",
        "Use after a snapshot to map one semantic element to its instrumented consumer component "
        "and DOM-host implementation. Returns explicit exact, heuristic, or unavailable confidence "
        "and only project-relative source locations. It may read a bounded local file to validate "
        "coordinates, but never returns absolute paths or source contents. CSS declarations "
        "remain available through frontend_inspect, "
        "but v0.3.0 "
        "does not map transformed stylesheets or tokens back to source.",
        [*_FRONTEND_READ, "files:read"],
        "Resolving frontend source…",
        "Frontend source ready",
        read=True,
        idempotent=True,
    ),
    "frontend_session_stop": _contract(
        "Stop frontend session",
        "Use when managed frontend work is complete to close its browser context and release owned "
        "development-server resources. Repeating stop for the same session is safe and reports "
        "already_stopped instead of creating or controlling another process.",
        _FRONTEND_INTERACT,
        "Stopping frontend session…",
        "Frontend session stopped",
        destructive=True,
        idempotent=True,
    ),
    # -----------------------------------------------------------------------
    # Code intelligence tools (language-neutral LSP/graph-backed)
    # -----------------------------------------------------------------------
    "code_intelligence_status": _contract(
        "Code intelligence status",
        "Use before semantic code tools when provider capability or freshness is unknown. Report "
        "the active workspace state, provider, capabilities, and freshness for a registered "
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
        "Use for a fast structural orientation before manually listing or reading many files. "
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
        "Use to find named classes, functions, methods, types, and other code symbols; use "
        "text_search instead for arbitrary strings. Search workspace or document symbols by name. "
        "Returns bounded matches with kind, "
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
        "Use when the source position is known and the symbol's declaration is needed; do not "
        "approximate this with text_search. Resolve the semantic definition at a 1-based "
        "line/character position. "
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
        "Use to find semantic uses of a known symbol before refactoring; use text_search only for "
        "literal text. Find references to the symbol at a 1-based position. "
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
        "Use to locate concrete implementations of a known interface or abstract symbol; do not "
        "substitute a filename/text search. Resolve from a 1-based position. "
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
        "Use for current editor/compiler diagnostics of one source file; use execute_shell for a "
        "real build or test run. Return language-server diagnostics for a project-relative file. "
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
        "Use for a symbol's signature, inferred type, or documentation at a known source position. "
        "Return hover information at a 1-based position. "
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
        "Use when one known symbol needs definition, hover, references, nearby source, and "
        "diagnostics together; prefer focused tools when only one result is needed. Aggregate from "
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
        "Use to answer who calls a known callable or what it calls; do not infer this from plain "
        "text matches. Prepare at a 1-based position and traverse incoming/outgoing calls. "
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
        "Use to answer inheritance/interface hierarchy for a known type. Prepare at a 1-based "
        "position and traverse supertypes/subtypes. "
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
        "Use before changing a file or symbol when downstream structural dependents are needed. "
        "Return bounded graph dependents with evidence and precision. "
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
        "Use for package/layer architecture instead of manually reading every manifest or running "
        "shell discovery commands. Return a structural graph from manifests and graph analysis. "
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
        "Use to inspect project/package dependencies without invoking package-manager listing "
        "commands. Return manifest-backed or graph-backed dependencies and direction. "
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
        "Use to discover likely tests for a known source file; use execute_shell only when "
        "actually running tests. Return related test files/functions. Relationship is exact "
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
        "Use only when semantic results are stale/unavailable and status indicates a refresh is "
        "appropriate. Explicitly trigger incremental or full refresh of the code intelligence "
        "index for a registered project. Does not acknowledge completion until indexing finishes. "
        "scope=incremental updates changed files; scope=full rebuilds from scratch. "
        "Reports timed_out=true if timeout_seconds is reached before completion.",
        _ANALYZE,
        "Reindexing workspace…",
        "Reindex complete",
        read=True,
        idempotent=False,
    ),
}
