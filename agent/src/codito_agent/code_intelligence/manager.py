"""CodeIntelligenceManager: main entry point for the code_intelligence subsystem.

The manager:
- Maintains one ProjectWorkspace per project_id (lazy-created on first use)
- Loads per-project LSP server profiles from {lsp_config_dir}/{project_id}.json
- Runs a periodic background task that shuts down idle LSP server processes
- Dispatches all 16 ProjectCodeInput operations to the appropriate provider
- Returns typed ProjectCodeResult values; never returns stale data after file change

Security:
- Project root is supplied by the agent database (trusted local state), not by
  the incoming request payload.
- LSP server command is read from agent-owned lsp_config_dir, never from repo
  files or relay payloads.
- No source bodies, absolute paths, or secrets appear in result payloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codito_protocol.code import (
    CapabilityInfo,
    CodeCallHierarchyResult,
    CodeContextResult,
    CodeDefinitionResult,
    CodeDiagnosticsResult,
    CodeHoverResult,
    CodeImplementationsResult,
    CodeIntelligenceStatusResult,
    CodeReferencesResult,
    CodeReindexResult,
    CodeSymbolSearchResult,
    CodeTypeHierarchyResult,
    CodeWorkspaceSummaryResult,
    ContextSection,
    ContextSections,
    DiagnosticSeverity,
    Precision,
    ProviderKind,
    SymbolInfo,
    WorkspaceReadyState,
)

from ..errors import AgentError
from .cbm_provider import CodebaseMemoryError, CodebaseMemoryGraphProvider
from .graph_provider import NullGraphProvider
from .lsp_client import LspClientError, LspServerProfile
from .lsp_provider import build_workspace_summary
from .source import read_source_text, source_snapshot
from .types import WorkspaceSnapshot, WorkspaceStatus
from .workspace import IDLE_TTL_SECONDS, ProjectWorkspace

if TYPE_CHECKING:
    from codito_protocol.code import ProjectCodeInput, ProjectCodeResult

logger = logging.getLogger(__name__)

_IDLE_CHECK_INTERVAL = 60.0  # seconds between idle checks


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _unavailable_snapshot_id() -> str:
    import secrets

    return secrets.token_hex(12)


# ---------------------------------------------------------------------------
# LSP profile loader (reads from agent-owned config directory)
# ---------------------------------------------------------------------------


def _load_lsp_profile(lsp_config_dir: Path, project_id: str) -> LspServerProfile | None:
    """Read per-project LSP server config from agent-owned config dir.

    File format: JSON object with keys:
      command  (required) list of str
      name     (optional) str
      version  (optional) str
      initialization_options (optional) dict
      workspace_configuration (optional) dict
    """
    config_path = lsp_config_dir / f"{project_id}.json"
    if not config_path.is_file():
        return None
    try:
        data: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        command = data.get("command")
        if not isinstance(command, list) or not command:
            logger.warning("LSP config for %s has no valid 'command'; skipping", project_id)
            return None
        return LspServerProfile(
            command=[str(c) for c in command],
            name=str(data.get("name", "lsp-server")),
            version=str(data.get("version", "0.0.0")),
            initialization_options=data.get("initialization_options") or {},
            workspace_configuration=data.get("workspace_configuration") or {},
        )
    except Exception as exc:
        logger.warning("Failed to load LSP config for %s: %s", project_id, exc)
        return None


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


def _workspace_state(ws: ProjectWorkspace) -> WorkspaceReadyState:
    status = ws.status
    if status == WorkspaceStatus.READY:
        return WorkspaceReadyState.READY
    if status in (WorkspaceStatus.INITIALIZING, WorkspaceStatus.INDEXING):
        return WorkspaceReadyState.INDEXING
    if status == WorkspaceStatus.NOT_STARTED:
        return WorkspaceReadyState.NOT_CONFIGURED
    return WorkspaceReadyState.UNAVAILABLE


def _capability_info(ws: ProjectWorkspace) -> CapabilityInfo:
    info = ws.capability_info()
    if not ws.graph_ready:
        return info
    # Structural graph coverage keeps navigation/relationship tools available on
    # languages without a local semantic server. Diagnostics remains LSP-only.
    return CapabilityInfo(
        definition=True,
        references=True,
        implementations=True,
        hover=True,
        diagnostics=info.diagnostics,
        document_symbols=True,
        workspace_symbols=True,
        call_hierarchy=True,
        type_hierarchy=True,
    )


# ---------------------------------------------------------------------------
# Source context helper
# ---------------------------------------------------------------------------


def _read_source_lines(root: Path, rel: str, center_line: int, context: int) -> str | None:
    """Read source lines centred at center_line (1-based) with ±context lines."""
    lines = read_source_text(root, rel).split("\n")
    total = len(lines)
    first = max(0, center_line - 1 - context)
    last = min(total, center_line - 1 + context + 1)
    return "\n".join(lines[first:last])


# ---------------------------------------------------------------------------
# Main manager class
# ---------------------------------------------------------------------------


class CodeIntelligenceManager:
    """Manages per-project workspaces and dispatches code intelligence operations."""

    def __init__(self, data_directory: Path, lsp_config_dir: Path | None = None) -> None:
        self._data_directory = data_directory
        self._lsp_config_dir = lsp_config_dir
        self._workspaces: dict[str, ProjectWorkspace] = {}
        self._idle_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the idle-TTL background reaper."""
        loop = asyncio.get_event_loop()
        self._idle_task = loop.create_task(self._idle_reaper(), name="codito-ci-idle-reaper")

    async def close(self) -> None:
        """Shut down all workspaces and stop the idle reaper."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
        for project_id, ws in list(self._workspaces.items()):
            try:
                await ws.shutdown()
            except Exception as exc:
                logger.debug("Error shutting down workspace %s: %s", project_id, exc)
        self._workspaces.clear()

    # ------------------------------------------------------------------
    # Main dispatch
    # ------------------------------------------------------------------

    async def execute(
        self,
        inp: ProjectCodeInput,
        project_root: Path,
        *,
        root_identity: str | None = None,
    ) -> ProjectCodeResult:
        ws = self._get_or_create_workspace(inp.project_id, project_root, root_identity)
        async with ws.query_lock:
            ws.touch()
            result: ProjectCodeResult
            try:
                if inp.operation == "code_reindex":
                    result = await self._reindex(ws, inp)
                    if (
                        result.completed
                        and ws.snapshot is not None
                        and await ws.snapshot_is_current(ws.snapshot)
                    ):
                        return self._verified(result)
                    result.freshness = "stale"
                    result.complete = False
                    return result
                metadata = inp.operation in {"code_intelligence_status", "code_workspace_summary"}
                for _attempt in range(2):
                    try:
                        snapshot = await ws.ensure_ready(analyze=not metadata)
                        rel = getattr(inp, "path", None)
                        if rel is not None and rel not in snapshot.file_states:
                            allow_directory = inp.operation == "code_dependencies"
                            if not allow_directory or not any(
                                path.startswith(rel.rstrip("/") + "/")
                                for path in snapshot.file_states
                            ):
                                raise AgentError(
                                    "unsafe_path",
                                    "Requested source is absent or excluded from analysis",
                                )
                        with source_snapshot(snapshot):
                            if inp.operation == "code_intelligence_status":
                                result = await self._status(ws, inp)
                            else:
                                result = await self._execute_ready(ws, inp, project_root, snapshot)
                        if await ws.snapshot_is_current(snapshot):
                            return self._verified(result)
                    except AgentError as exc:
                        if exc.code != "path_race":
                            raise
                raise AgentError(
                    "path_race",
                    "Workspace changed repeatedly during analysis; retry",
                    retryable=True,
                )
            except (LspClientError, CodebaseMemoryError) as exc:
                await asyncio.shield(ws.shutdown())
                raise AgentError(
                    "analysis_unavailable",
                    "Code-analysis provider could not complete this snapshot",
                    retryable=True,
                ) from exc
            except BaseException:
                await asyncio.shield(ws.shutdown())
                raise

    @staticmethod
    def _verified(result: ProjectCodeResult) -> ProjectCodeResult:
        result.freshness = "verified"
        result.verified_at = _now_iso()
        # Exact precision describes the returned locations/types; it does not prove
        # that a language server exhaustively indexed the whole workspace. Some
        # providers (notably Pyright) can return exact but incomplete references
        # while their background index is still lazy. Keep workspace-wide/bounded
        # operations incomplete unless their contract has an explicit completeness
        # condition below.
        result.complete = False
        if isinstance(result, (CodeDefinitionResult, CodeHoverResult)):
            result.complete = result.precision == Precision.EXACT and not result.warnings
        elif isinstance(result, CodeDiagnosticsResult):
            if str(result.state) != "ready":
                result.freshness = "unknown"
            else:
                result.complete = (
                    result.precision == Precision.EXACT
                    and not result.truncated
                    and not result.warnings
                )
        elif isinstance(result, CodeReindexResult):
            result.complete = result.completed
        elif isinstance(result, CodeWorkspaceSummaryResult):
            result.complete = len(result.manifests) < 128
        elif isinstance(result, CodeContextResult):
            for section in (result.definition, result.hover, result.references, result.diagnostics):
                if section is not None:
                    CodeIntelligenceManager._verified(section)
            result.complete = False  # Inspect per-section completeness; aggregation is bounded.
        return result

    async def _execute_ready(
        self,
        ws: ProjectWorkspace,
        inp: Any,
        project_root: Path,
        snapshot: WorkspaceSnapshot,
    ) -> ProjectCodeResult:
        op = inp.operation
        graph = ws.graph_provider
        structural = (
            graph if isinstance(graph, CodebaseMemoryGraphProvider) and ws.graph_ready else None
        )
        snap_id = snapshot.generation

        if op == "code_workspace_summary":
            return build_workspace_summary(snapshot, project_root)

        if op == "code_symbol_search":
            if inp.scope == "document":
                if inp.path is None:
                    raise AgentError("invalid_request", "Document symbol search requires a path")
                async with ws.lsp_for_document(inp.path) as lsp:
                    if lsp is not None and lsp.capabilities.document_symbols:
                        return await lsp.document_symbols(
                            inp.path, inp.query, inp.kinds, inp.max_results, snap_id
                        )
                if structural is not None:
                    return await structural.symbol_search(
                        inp.query,
                        path=inp.path,
                        kinds=inp.kinds,
                        max_results=inp.max_results,
                        snapshot_id=snap_id,
                    )
            else:
                providers = await ws.lsp_for_workspace(snapshot.file_states)
                exact_results: list[CodeSymbolSearchResult] = []
                for lsp in providers:
                    if not lsp.capabilities.workspace_symbols:
                        continue
                    exact_results.append(
                        await lsp.workspace_symbols(inp.query, inp.kinds, inp.max_results, snap_id)
                    )
                if exact_results:
                    merged: list[SymbolInfo] = []
                    warnings: list[str] = []
                    seen: set[tuple[object, ...]] = set()
                    total_matches = 0
                    truncated = False
                    for result in exact_results:
                        total_matches += result.total_matches
                        truncated = truncated or result.truncated
                        warnings.extend(result.warnings)
                        for symbol in result.symbols:
                            rng = symbol.location.range
                            key = (
                                symbol.name,
                                symbol.kind,
                                symbol.location.path,
                                rng.start.line if rng else None,
                                rng.start.character if rng else None,
                            )
                            if key in seen:
                                continue
                            seen.add(key)
                            if len(merged) < inp.max_results:
                                merged.append(symbol)
                            else:
                                truncated = True
                    return CodeSymbolSearchResult(
                        snapshot_id=snap_id,
                        provider=ProviderKind.LSP,
                        precision=Precision.EXACT,
                        captured_at=_now_iso(),
                        symbols=merged,
                        total_matches=max(total_matches, len(seen)),
                        truncated=truncated,
                        warnings=warnings[:16],
                    )
                if structural is not None:
                    return await structural.symbol_search(
                        inp.query,
                        path=None,
                        kinds=inp.kinds,
                        max_results=inp.max_results,
                        snapshot_id=snap_id,
                    )
            return CodeSymbolSearchResult(
                snapshot_id=snap_id,
                provider=ProviderKind.NONE,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["No compatible semantic or structural symbol provider is available"],
            )

        if op == "code_definition":
            async with ws.lsp_for_document(inp.path) as lsp:
                if lsp is not None and lsp.capabilities.definition:
                    return await lsp.definition(inp.path, inp.line, inp.character, snap_id)
            if structural is not None:
                return await structural.definition(inp.path, inp.line, snap_id)
            return CodeDefinitionResult(
                snapshot_id=snap_id,
                provider=ProviderKind.NONE,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["Definition is unavailable for this language"],
            )

        if op == "code_references":
            exact_references: CodeReferencesResult | None = None
            async with ws.lsp_for_document(inp.path) as lsp:
                if lsp is not None and lsp.capabilities.references:
                    exact_references = await lsp.references(
                        inp.path,
                        inp.line,
                        inp.character,
                        inp.include_declaration,
                        inp.max_results,
                        snap_id,
                    )
                    if exact_references.references:
                        return exact_references
            if structural is not None:
                structural_references = await structural.references(
                    inp.path, inp.line, inp.include_declaration, inp.max_results, snap_id
                )
                if exact_references is not None:
                    structural_references.warnings.insert(
                        0,
                        "Semantic provider returned no references; returning structural candidates",
                    )
                return structural_references
            if exact_references is not None:
                exact_references.warnings.append(
                    "Semantic provider returned an empty reference set; no structural provider was "
                    "available for cross-checking"
                )
                return exact_references
            return CodeReferencesResult(
                snapshot_id=snap_id,
                provider=ProviderKind.NONE,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["References are unavailable for this language"],
            )

        if op == "code_implementations":
            async with ws.lsp_for_document(inp.path) as lsp:
                if lsp is not None and lsp.capabilities.implementations:
                    return await lsp.implementations(
                        inp.path, inp.line, inp.character, inp.max_results, snap_id
                    )
            if structural is not None:
                return await structural.implementations(
                    inp.path, inp.line, inp.max_results, snap_id
                )
            return CodeImplementationsResult(
                snapshot_id=snap_id,
                provider=ProviderKind.NONE,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["Implementations are unavailable for this language"],
            )

        if op == "code_diagnostics":
            async with ws.lsp_for_document(inp.path) as lsp:
                if lsp is not None:
                    return await lsp.diagnostics(
                        inp.path, DiagnosticSeverity(inp.severity_min), inp.max_results, snap_id
                    )
            return CodeDiagnosticsResult(
                snapshot_id=snap_id,
                provider=ProviderKind.NONE,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["Diagnostics require a compatible local language server"],
            )

        if op == "code_hover":
            async with ws.lsp_for_document(inp.path) as lsp:
                if lsp is not None and lsp.capabilities.hover:
                    return await lsp.hover(inp.path, inp.line, inp.character, snap_id)
            if structural is not None:
                return await structural.hover(inp.path, inp.line, snap_id)
            return CodeHoverResult(
                snapshot_id=snap_id,
                provider=ProviderKind.NONE,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["Hover information is unavailable for this language"],
            )

        if op == "code_context":
            return await self._context(ws, inp, project_root, snapshot)

        if op == "code_call_hierarchy":
            async with ws.lsp_for_document(inp.path) as lsp:
                if lsp is not None and lsp.capabilities.call_hierarchy:
                    return await lsp.call_hierarchy(
                        inp.path,
                        inp.line,
                        inp.character,
                        inp.direction,
                        inp.max_depth,
                        inp.max_nodes,
                        snap_id,
                    )
            if structural is not None:
                return await structural.call_hierarchy(
                    inp.path, inp.line, inp.direction, inp.max_depth, inp.max_nodes, snap_id
                )
            return CodeCallHierarchyResult(
                snapshot_id=snap_id,
                provider=ProviderKind.NONE,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["Call hierarchy is unavailable for this language"],
            )

        if op == "code_type_hierarchy":
            async with ws.lsp_for_document(inp.path) as lsp:
                if lsp is not None and lsp.capabilities.type_hierarchy:
                    return await lsp.type_hierarchy(
                        inp.path,
                        inp.line,
                        inp.character,
                        inp.direction,
                        inp.max_depth,
                        inp.max_nodes,
                        snap_id,
                    )
            if structural is not None:
                return await structural.type_hierarchy(
                    inp.path,
                    inp.line,
                    inp.direction,
                    inp.max_nodes,
                    snap_id,
                    max_depth=inp.max_depth,
                )
            return CodeTypeHierarchyResult(
                snapshot_id=snap_id,
                provider=ProviderKind.NONE,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["Type hierarchy is unavailable for this language"],
            )

        if op == "code_impact":
            return await (graph if ws.graph_ready else NullGraphProvider()).impact(
                inp.path, snapshot, inp.max_nodes, snap_id
            )

        if op == "code_architecture":
            return await (graph if ws.graph_ready else NullGraphProvider()).architecture(
                snapshot, inp.max_packages, snap_id
            )

        if op == "code_dependencies":
            return await (graph if ws.graph_ready else NullGraphProvider()).dependencies(
                inp.path, snapshot, inp.direction, inp.max_results, snap_id
            )

        if op == "code_related_tests":
            return await (graph if ws.graph_ready else NullGraphProvider()).related_tests(
                inp.path, snapshot, inp.max_results, snap_id
            )

        raise AgentError("invalid_request", f"Unknown code operation: {op!r}")

    # ------------------------------------------------------------------
    # Operation implementations
    # ------------------------------------------------------------------

    async def _status(self, ws: ProjectWorkspace, inp: Any) -> CodeIntelligenceStatusResult:
        snapshot = ws.snapshot
        snap_id = snapshot.generation if snapshot else _unavailable_snapshot_id()
        indexed = len(snapshot.file_states) if snapshot else 0
        state = _workspace_state(ws)
        caps = _capability_info(ws)
        warnings: list[str] = []
        if ws.startup_error:
            warnings.append(ws.startup_error[:256])
        setup_requirements: list[str] = []
        if not ws.graph_available:
            setup_requirements.append(
                "Install the Codito structural code-intelligence runtime to enable "
                "language-neutral symbol, relationship, impact, and architecture analysis."
            )
        if ws.lsp_server_name is None:
            setup_requirements.append(
                "Diagnostics and exact semantic navigation require a compatible local "
                "language server; Codito auto-detects supported servers from the trusted PATH."
            )
        provider = (
            ProviderKind.GRAPH
            if ws.graph_available
            else ProviderKind.LSP
            if ws.lsp_server_name is not None
            else ProviderKind.MANIFEST
        )
        precision = (
            Precision.EXACT
            if ws.lsp_server_name is not None
            else Precision.STRUCTURAL
            if ws.graph_available
            else Precision.HEURISTIC
        )
        return CodeIntelligenceStatusResult(
            snapshot_id=snap_id,
            provider=provider,
            precision=precision,
            captured_at=_now_iso(),
            status=state,
            lsp_server=ws.lsp_server_name,
            capabilities=caps,
            indexed_files=indexed,
            setup_requirements=setup_requirements,
            warnings=warnings,
        )

    async def _context(
        self,
        ws: ProjectWorkspace,
        inp: Any,
        project_root: Path,
        snapshot: WorkspaceSnapshot,
    ) -> CodeContextResult:
        snap_id = snapshot.generation
        structural = (
            ws.graph_provider
            if isinstance(ws.graph_provider, CodebaseMemoryGraphProvider) and ws.graph_ready
            else None
        )

        definition: CodeDefinitionResult | None = None
        hover: CodeHoverResult | None = None
        references: CodeReferencesResult | None = None
        diagnostics_result: CodeDiagnosticsResult | None = None
        source_lines: str | None = None

        # Source lines
        if inp.source_context_lines > 0:
            source_lines = _read_source_lines(
                project_root, inp.path, inp.line, inp.source_context_lines
            )

        def _sec(available: bool, precision: Precision, provider: ProviderKind) -> ContextSection:
            return ContextSection(available=available, precision=precision, provider=provider)

        def_sec = _sec(False, Precision.UNAVAILABLE, ProviderKind.NONE)
        hover_sec = _sec(False, Precision.UNAVAILABLE, ProviderKind.NONE)
        ref_sec = _sec(False, Precision.UNAVAILABLE, ProviderKind.NONE)
        diag_sec = _sec(False, Precision.UNAVAILABLE, ProviderKind.NONE)
        src_sec = _sec(source_lines is not None, Precision.EXACT, ProviderKind.MANIFEST)

        used_exact = False
        async with ws.lsp_for_document(inp.path) as lsp:
            if lsp is not None:
                if lsp.capabilities.definition:
                    definition = await lsp.definition(inp.path, inp.line, inp.character, snap_id)
                    def_sec = _sec(
                        definition.precision != Precision.UNAVAILABLE,
                        definition.precision,
                        ProviderKind.LSP,
                    )
                    used_exact = used_exact or bool(definition.definitions)

                if lsp.capabilities.hover:
                    hover = await lsp.hover(inp.path, inp.line, inp.character, snap_id)
                    hover_sec = _sec(hover.content is not None, Precision.EXACT, ProviderKind.LSP)
                    used_exact = used_exact or hover.content is not None

                if inp.include_references and lsp.capabilities.references:
                    references = await lsp.references(
                        inp.path,
                        inp.line,
                        inp.character,
                        include_declaration=True,
                        max_results=50,
                        snapshot_id=snap_id,
                    )
                    ref_sec = _sec(
                        references.precision != Precision.UNAVAILABLE,
                        references.precision,
                        ProviderKind.LSP,
                    )
                    used_exact = used_exact or bool(references.references)

                if inp.include_diagnostics:
                    diagnostics_result = await lsp.diagnostics(
                        inp.path, DiagnosticSeverity.INFORMATION, 50, snap_id
                    )
                    diag_sec = _sec(
                        str(diagnostics_result.state) == "ready",
                        diagnostics_result.precision,
                        ProviderKind.LSP,
                    )
                    used_exact = True

        if structural is not None:
            if definition is None:
                definition = await structural.definition(inp.path, inp.line, snap_id)
                def_sec = _sec(
                    bool(definition.definitions), definition.precision, definition.provider
                )
            if hover is None:
                hover = await structural.hover(inp.path, inp.line, snap_id)
                hover_sec = _sec(hover.content is not None, hover.precision, hover.provider)
            if inp.include_references and references is None:
                references = await structural.references(inp.path, inp.line, True, 50, snap_id)
                ref_sec = _sec(
                    bool(references.references), references.precision, references.provider
                )

        sections = ContextSections(
            definition=def_sec,
            hover=hover_sec,
            references=ref_sec,
            diagnostics=diag_sec,
            source_lines=src_sec,
        )

        return CodeContextResult(
            snapshot_id=snap_id,
            provider=(
                ProviderKind.LSP
                if used_exact
                else ProviderKind.GRAPH
                if structural is not None
                else ProviderKind.MANIFEST
            ),
            precision=(
                Precision.EXACT
                if used_exact
                else Precision.STRUCTURAL
                if structural is not None
                else Precision.HEURISTIC
            ),
            captured_at=_now_iso(),
            definition=definition,
            hover=hover,
            references=references,
            diagnostics=diagnostics_result,
            source_lines=source_lines,
            source_start_line=max(1, inp.line - inp.source_context_lines)
            if source_lines is not None
            else None,
            sections=sections,
        )

    async def _reindex(self, ws: ProjectWorkspace, inp: Any) -> CodeReindexResult:
        from .types import _new_generation

        full = inp.scope == "full"
        errors: list[str] = []
        timed_out = False
        files_processed = 0
        snapshot: WorkspaceSnapshot | None = None

        try:
            snapshot = await asyncio.wait_for(
                ws.force_reindex(full=full),
                timeout=float(inp.timeout_seconds),
            )
            files_processed = len(snapshot.file_states)
        except TimeoutError:
            timed_out = True
            snapshot = ws.snapshot
            files_processed = len(snapshot.file_states) if snapshot else 0
            errors.append("Reindex timed out; partial results may be available")
        except Exception:
            errors.append("Reindex failed due to a local code-intelligence provider error")
            snapshot = ws.snapshot

        if ws.graph_error and ws.graph_available:
            errors.append(ws.graph_error)

        snap_id = snapshot.generation if snapshot else _new_generation()
        return CodeReindexResult(
            snapshot_id=snap_id,
            provider=ProviderKind.GRAPH if ws.graph_available else ProviderKind.MANIFEST,
            precision=Precision.STRUCTURAL,
            captured_at=_now_iso(),
            completed=not timed_out and not errors,
            generation=snap_id,
            files_processed=files_processed,
            errors=errors,
            timed_out=timed_out,
        )

    # ------------------------------------------------------------------
    # Workspace registry
    # ------------------------------------------------------------------

    def _get_or_create_workspace(
        self, project_id: str, project_root: Path, root_identity: str | None = None
    ) -> ProjectWorkspace:
        if project_id in self._workspaces:
            existing = self._workspaces[project_id]
            if existing.project_root != project_root or existing.root_identity != root_identity:
                raise AgentError("project_identity_changed", "Analysis workspace identity changed")
            return existing
        lsp_profile = (
            _load_lsp_profile(self._lsp_config_dir, project_id)
            if self._lsp_config_dir is not None
            else None
        )
        ws = ProjectWorkspace(
            project_id=project_id,
            project_root=project_root,
            root_identity=root_identity,
            lsp_profile=lsp_profile,
            graph_provider=CodebaseMemoryGraphProvider(project_root, self._data_directory),
        )
        self._workspaces[project_id] = ws
        logger.debug(
            "Created workspace for project %s (lsp=%s)",
            project_id,
            lsp_profile.name if lsp_profile else None,
        )
        return ws

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    async def _idle_reaper(self) -> None:
        while True:
            try:
                await asyncio.sleep(_IDLE_CHECK_INTERVAL)
            except asyncio.CancelledError:
                return
            idle_ids = [
                pid
                for pid, ws in self._workspaces.items()
                if ws.is_idle(IDLE_TTL_SECONDS)
                and ws.status not in (WorkspaceStatus.INITIALIZING, WorkspaceStatus.SHUTTING_DOWN)
            ]
            for project_id in idle_ids:
                ws = self._workspaces.get(project_id)
                if ws is not None and ws.is_idle(IDLE_TTL_SECONDS):
                    logger.debug("Shutting down idle workspace for project %s", project_id)
                    try:
                        async with ws.query_lock:
                            await ws.shutdown()
                    except Exception as exc:
                        logger.debug("Idle shutdown error for %s: %s", project_id, exc)
