"""LSP-backed code intelligence provider.

Translates Codito public operations to LSP calls via ManagedLspClient.
Handles:
  - Position conversion: 1-based Unicode scalar ↔ 0-based LSP UTF-16 code units
  - URI ↔ project-relative path mapping (with normalization; rejects external URIs)
  - LSP result → Codito result model conversion
  - Capability unavailable → explicit typed response (never empty success)

All returned paths are validated project-relative. External library URIs
(outside the workspace root) are reported as external_unavailable in warnings.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from codito_protocol.code import (
    CallItem,
    CapabilityInfo,
    CodeCallHierarchyResult,
    CodeDefinitionResult,
    CodeDiagnostic,
    CodeDiagnosticsResult,
    CodeHoverResult,
    CodeImplementationsResult,
    CodeLocation,
    CodePosition,
    CodeRange,
    CodeReferencesResult,
    CodeSymbolSearchResult,
    CodeTypeHierarchyResult,
    CodeWorkspaceSummaryResult,
    DiagnosticSeverity,
    DiagnosticState,
    LanguageSummary,
    ManifestInfo,
    Precision,
    ProviderKind,
    SymbolInfo,
    SymbolKind,
    TypeItem,
)
from lsprotocol import types as lsp

from ..errors import AgentError
from ..paths import ProjectPathResolver
from .lsp_client import ManagedLspClient
from .snapshot import _safe_project
from .source import current_snapshot, read_source_text
from .types import CapabilitySet, WorkspaceSnapshot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Position conversion: public (1-based Unicode scalar) ↔ LSP (0-based UTF-16)
# ---------------------------------------------------------------------------


def unicode_scalar_to_utf16_offset(line_text: str, scalar_col: int) -> int:
    """Convert 1-based Unicode scalar column to 0-based UTF-16 code-unit offset.

    scalar_col=1 maps to the first character of the line.
    Astral characters (U+10000+) consume 2 UTF-16 code units.
    """
    if scalar_col < 1 or scalar_col > len(line_text) + 1:
        raise AgentError("invalid_request", "Column is outside the current source line")
    target = scalar_col - 1  # 0-based scalar index
    utf16 = 0
    for i, ch in enumerate(line_text):
        if i >= target:
            break
        if ord(ch) >= 0x10000:
            utf16 += 2
        else:
            utf16 += 1
    return utf16


def utf16_offset_to_unicode_scalar(line_text: str, utf16_offset: int) -> int:
    """Convert 0-based UTF-16 code-unit offset to 1-based Unicode scalar column."""
    if utf16_offset < 0:
        raise AgentError("invalid_request", "Language-server source offset is negative")
    scalar = 0
    u16 = 0
    for ch in line_text:
        if u16 == utf16_offset:
            break
        width = 2 if ord(ch) >= 0x10000 else 1
        if u16 + width > utf16_offset:
            raise AgentError(
                "invalid_request", "Language-server source offset splits a Unicode character"
            )
        u16 += width
        scalar += 1
    if u16 != utf16_offset:
        raise AgentError("invalid_request", "Language-server source offset is outside the line")
    return scalar + 1  # 1-based


def lsp_position_from_public(line_text: str, pub_line: int, pub_character: int) -> lsp.Position:
    """Convert public 1-based (line, character) to LSP 0-based UTF-16 position.

    line_text must be the text of pub_line (used for Unicode scalar→UTF-16 mapping).
    """
    lsp_line = pub_line - 1  # 1-based → 0-based
    lsp_char = unicode_scalar_to_utf16_offset(line_text, pub_character)
    return lsp.Position(line=lsp_line, character=lsp_char)


def public_position_from_lsp(line_text: str, lsp_pos: lsp.Position) -> CodePosition:
    """Convert LSP 0-based UTF-16 position to public 1-based Unicode scalar position."""
    pub_line = lsp_pos.line + 1  # 0-based → 1-based
    pub_char = utf16_offset_to_unicode_scalar(line_text, lsp_pos.character)
    return CodePosition(line=pub_line, character=pub_char)


def public_range_from_lsp(start_text: str, end_text: str, lsp_range: lsp.Range) -> CodeRange:
    return CodeRange(
        start=public_position_from_lsp(start_text, lsp_range.start),
        end=public_position_from_lsp(end_text, lsp_range.end),
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# URI / path mapping
# ---------------------------------------------------------------------------


def uri_to_project_relative(uri: str, workspace_root: Path) -> str | None:
    """Convert a file:// URI to a project-relative POSIX path.

    Returns None if the URI is external to the workspace root.
    Rejects non-file URIs and reparse paths.
    """
    parsed = urlparse(uri)
    if (
        parsed.scheme != "file"
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    # Decode percent-encoding; Windows: /C:/... → C:/...
    raw_path = unquote(parsed.path)
    if os.name == "nt" and raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
        raw_path = raw_path[1:]  # strip leading /
    try:
        abs_path = Path(raw_path).resolve(strict=True)
        relative = abs_path.relative_to(workspace_root.resolve(strict=True)).as_posix()
        snapshot = current_snapshot()
        if snapshot is not None and relative not in snapshot.file_states:
            return None
        project = _safe_project(
            workspace_root,
            snapshot.root_identity if snapshot is not None else None,
        )
        ProjectPathResolver().resolve(project, relative, must_exist=True, directory=False)
    except (AgentError, OSError, ValueError):
        return None
    return relative


def project_relative_to_uri(relative: str, workspace_root: Path) -> str:
    return (workspace_root / relative).as_uri()


# ---------------------------------------------------------------------------
# LSP → Codito type conversion helpers
# ---------------------------------------------------------------------------

_LSP_SYMBOL_KIND_MAP: dict[int, SymbolKind] = {
    1: SymbolKind.FILE,
    2: SymbolKind.MODULE,
    3: SymbolKind.NAMESPACE,
    4: SymbolKind.PACKAGE,
    5: SymbolKind.CLASS,
    6: SymbolKind.METHOD,
    7: SymbolKind.PROPERTY,
    8: SymbolKind.FIELD,
    9: SymbolKind.CONSTRUCTOR,
    10: SymbolKind.ENUM,
    11: SymbolKind.INTERFACE,
    12: SymbolKind.FUNCTION,
    13: SymbolKind.VARIABLE,
    14: SymbolKind.CONSTANT,
    15: SymbolKind.STRING,
    16: SymbolKind.NUMBER,
    17: SymbolKind.BOOLEAN,
    18: SymbolKind.ARRAY,
    19: SymbolKind.OBJECT,
    20: SymbolKind.KEY,
    21: SymbolKind.NULL,
    22: SymbolKind.ENUM_MEMBER,
    23: SymbolKind.STRUCT,
    24: SymbolKind.EVENT,
    25: SymbolKind.OPERATOR,
    26: SymbolKind.TYPE_PARAMETER,
}

_LSP_DIAG_SEVERITY_MAP: dict[int | None, DiagnosticSeverity] = {
    1: DiagnosticSeverity.ERROR,
    2: DiagnosticSeverity.WARNING,
    3: DiagnosticSeverity.INFORMATION,
    4: DiagnosticSeverity.HINT,
    None: DiagnosticSeverity.HINT,
}

_SEVERITY_ORDER: dict[DiagnosticSeverity, int] = {
    DiagnosticSeverity.ERROR: 1,
    DiagnosticSeverity.WARNING: 2,
    DiagnosticSeverity.INFORMATION: 3,
    DiagnosticSeverity.HINT: 4,
}


def _lsp_symbol_kind(kind: lsp.SymbolKind | int | None) -> SymbolKind:
    if kind is None:
        return SymbolKind.UNKNOWN
    value = kind.value if isinstance(kind, lsp.SymbolKind) else int(kind)
    return _LSP_SYMBOL_KIND_MAP.get(value, SymbolKind.UNKNOWN)


def _lsp_severity(sev: lsp.DiagnosticSeverity | int | None) -> DiagnosticSeverity:
    if sev is None:
        return DiagnosticSeverity.HINT
    value = sev.value if isinstance(sev, lsp.DiagnosticSeverity) else int(sev)
    return _LSP_DIAG_SEVERITY_MAP.get(value, DiagnosticSeverity.HINT)


def _read_line(path: Path, line_0based: int) -> str:
    """Read a single line (0-based) from a file for position conversion."""
    try:
        snapshot = current_snapshot()
        root = snapshot.project_root if snapshot is not None else path.parent
        relative = path.relative_to(root).as_posix()
        lines = read_source_text(root, relative, snapshot).split("\n")
        if line_0based < 0 or line_0based >= len(lines):
            raise AgentError("invalid_request", "Code location is outside the source document")
        return lines[line_0based]
    except ValueError as exc:
        raise AgentError("unsafe_path", "Language server returned an external source path") from exc


def _lsp_location_to_code(loc: lsp.Location, workspace_root: Path) -> CodeLocation | None:
    rel = uri_to_project_relative(loc.uri, workspace_root)
    if rel is None:
        return None
    path = workspace_root / rel
    start_text = _read_line(path, loc.range.start.line)
    end_text = _read_line(path, loc.range.end.line)
    return CodeLocation(
        path=rel,
        range=public_range_from_lsp(start_text, end_text, loc.range),
    )


def _lsp_location_link_to_code(link: lsp.LocationLink, workspace_root: Path) -> CodeLocation | None:
    rel = uri_to_project_relative(link.target_uri, workspace_root)
    if rel is None:
        return None
    path = workspace_root / rel
    target_range = link.target_selection_range
    start_text = _read_line(path, target_range.start.line)
    end_text = _read_line(path, target_range.end.line)
    return CodeLocation(
        path=rel,
        range=public_range_from_lsp(start_text, end_text, target_range),
    )


def _normalize_lsp_locations(
    raw: Any, workspace_root: Path
) -> tuple[list[CodeLocation], list[str]]:
    """Normalise any LSP location result to a list of CodeLocation + external warnings."""
    if raw is None:
        return [], []
    locations: list[CodeLocation] = []
    warnings: list[str] = []

    items: list[Any] = [raw] if not isinstance(raw, list) else raw
    external_count = 0
    for item in items:
        if isinstance(item, lsp.Location):
            loc = _lsp_location_to_code(item, workspace_root)
        elif isinstance(item, lsp.LocationLink):
            loc = _lsp_location_link_to_code(item, workspace_root)
        else:
            continue
        if loc is None:
            external_count += 1
        else:
            locations.append(loc)

    if external_count > 0:
        warnings.append(f"{external_count} definition(s) in external libraries are not returned")
    return locations, warnings


# ---------------------------------------------------------------------------
# Workspace summary helper (no LSP required)
# ---------------------------------------------------------------------------

_LANG_EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".cs": "csharp",
    ".vb": "visualbasic",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".dart": "dart",
    ".lua": "lua",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".zsh": "shellscript",
    ".fish": "shellscript",
    ".ps1": "powershell",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
}

_MANIFEST_KIND_MAP: dict[str, str] = {
    "package.json": "npm",
    "package-lock.json": "npm-lock",
    "yarn.lock": "yarn-lock",
    "pnpm-lock.yaml": "pnpm-lock",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "uv.lock": "uv-lock",
    "Cargo.toml": "rust",
    "Cargo.lock": "rust-lock",
    "go.mod": "go",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    ".csproj": "dotnet",
    ".sln": "dotnet-solution",
    ".vbproj": "dotnet",
    ".fsproj": "dotnet",
    "Gemfile": "ruby",
    "Gemfile.lock": "ruby-lock",
}

_FRAMEWORK_HINTS: dict[str, list[str]] = {
    "next.config.js": ["Next.js"],
    "next.config.ts": ["Next.js"],
    "next.config.mjs": ["Next.js"],
    "vite.config.ts": ["Vite"],
    "vite.config.js": ["Vite"],
    "astro.config.mjs": ["Astro"],
    "svelte.config.js": ["Svelte"],
    "nuxt.config.ts": ["Nuxt"],
    "angular.json": ["Angular"],
    "remix.config.js": ["Remix"],
}


def build_workspace_summary(
    snapshot: WorkspaceSnapshot, workspace_root: Path
) -> CodeWorkspaceSummaryResult:
    """Analyse snapshot for languages, manifests, frameworks."""
    lang_counts: dict[str, dict[str, Any]] = {}
    manifests: list[ManifestInfo] = []
    frameworks: list[str] = []
    total_bytes = 0

    for rel_path, entry in snapshot.file_states.items():
        total_bytes += entry.size
        name = os.path.basename(rel_path)
        ext = os.path.splitext(name)[1].lower()
        lang = _LANG_EXTENSION_MAP.get(ext)
        if lang:
            if lang not in lang_counts:
                lang_counts[lang] = {"file_count": 0, "extensions": set()}
            lang_counts[lang]["file_count"] += 1
            lang_counts[lang]["extensions"].add(ext)

        # Manifests
        kind = _MANIFEST_KIND_MAP.get(name) or _MANIFEST_KIND_MAP.get(ext)
        if kind:
            manifests.append(ManifestInfo(path=rel_path, kind=kind))

        # Framework hints
        for hint_file, hints in _FRAMEWORK_HINTS.items():
            if name == hint_file:
                for hint in hints:
                    if hint not in frameworks:
                        frameworks.append(hint)

    # Also check package.json content for react/vue/angular
    if "package.json" in snapshot.file_states:
        try:
            pkg = json.loads(read_source_text(workspace_root, "package.json", snapshot))
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "react" in all_deps and "React" not in frameworks:
                frameworks.append("React")
            if "vue" in all_deps and "Vue" not in frameworks:
                frameworks.append("Vue")
            if "@angular/core" in all_deps and "Angular" not in frameworks:
                frameworks.append("Angular")
        except (json.JSONDecodeError, AgentError) as exc:
            logger.debug(
                "Ignoring unreadable package.json framework hints (%s)",
                type(exc).__name__,
            )

    languages = [
        LanguageSummary(
            language=lang,
            file_count=info["file_count"],
            extensions=sorted(info["extensions"]),
        )
        for lang, info in sorted(lang_counts.items(), key=lambda x: -x[1]["file_count"])
    ]

    return CodeWorkspaceSummaryResult(
        snapshot_id=snapshot.generation,
        provider=ProviderKind.MANIFEST,
        precision=Precision.STRUCTURAL,
        captured_at=_now_iso(),
        languages=languages,
        manifests=manifests[:128],
        frameworks=frameworks,
        build_roots=sorted(
            {
                parent if parent != "." else manifest.path
                for manifest in manifests
                if (parent := Path(manifest.path).parent.as_posix())
            }
        )[:32],
        total_files=len(snapshot.file_states),
        total_bytes=total_bytes,
    )


# ---------------------------------------------------------------------------
# Main provider class
# ---------------------------------------------------------------------------


class LspProvider:
    """Wraps a ManagedLspClient and translates calls to Codito result models."""

    def __init__(self, client: ManagedLspClient, workspace_root: Path) -> None:
        self._client = client
        self._root = workspace_root

    @property
    def capabilities(self) -> CapabilitySet:
        return self._client.capabilities

    def _uri(self, rel: str) -> str:
        return project_relative_to_uri(rel, self._root)

    def _line_text(self, rel: str, lsp_line: int) -> str:
        return _read_line(self._root / rel, lsp_line)

    def _lsp_line_char(self, rel: str, pub_line: int, pub_char: int) -> tuple[int, int]:
        """Convert public position to LSP position for a given file."""
        lsp_line = pub_line - 1
        line_text = self._line_text(rel, lsp_line)
        lsp_char = unicode_scalar_to_utf16_offset(line_text, pub_char)
        return lsp_line, lsp_char

    def capability_info(self) -> CapabilityInfo:
        caps = self._client.capabilities
        return CapabilityInfo(
            definition=caps.definition,
            references=caps.references,
            implementations=caps.implementations,
            hover=caps.hover,
            diagnostics=caps.diagnostics_pull or caps.diagnostics_push,
            document_symbols=caps.document_symbols,
            workspace_symbols=caps.workspace_symbols,
            call_hierarchy=caps.call_hierarchy,
            type_hierarchy=caps.type_hierarchy,
        )

    async def _workspace_ready_warnings(self) -> list[str]:
        if await self._client.wait_for_analysis_ready():
            return []
        return [
            "Language server is still loading the workspace; returned semantic items are exact "
            "but the workspace result may be incomplete"
        ]

    async def definition(
        self, rel: str, pub_line: int, pub_char: int, snapshot_id: str
    ) -> CodeDefinitionResult:
        lsp_line, lsp_char = self._lsp_line_char(rel, pub_line, pub_char)
        raw = await self._client.request_definition(self._uri(rel), lsp_line, lsp_char)
        locations, warnings = _normalize_lsp_locations(raw, self._root)
        return CodeDefinitionResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            definitions=locations,
            warnings=warnings,
        )

    async def references(
        self,
        rel: str,
        pub_line: int,
        pub_char: int,
        include_declaration: bool,
        max_results: int,
        snapshot_id: str,
    ) -> CodeReferencesResult:
        readiness_warnings = await self._workspace_ready_warnings()
        lsp_line, lsp_char = self._lsp_line_char(rel, pub_line, pub_char)
        raw = await self._client.request_references(
            self._uri(rel), lsp_line, lsp_char, include_declaration
        )
        if raw is None:
            return CodeReferencesResult(
                snapshot_id=snapshot_id,
                provider=ProviderKind.LSP,
                precision=Precision.EXACT,
                captured_at=_now_iso(),
                warnings=["References request returned no data"],
            )
        locs, warnings = _normalize_lsp_locations(raw, self._root)
        warnings = [*readiness_warnings, *warnings]
        total = len(locs)
        truncated = total > max_results
        return CodeReferencesResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            references=locs[:max_results],
            total_matches=total,
            truncated=truncated,
            warnings=warnings,
        )

    async def implementations(
        self, rel: str, pub_line: int, pub_char: int, max_results: int, snapshot_id: str
    ) -> CodeImplementationsResult:
        readiness_warnings = await self._workspace_ready_warnings()
        lsp_line, lsp_char = self._lsp_line_char(rel, pub_line, pub_char)
        raw = await self._client.request_implementation(self._uri(rel), lsp_line, lsp_char)
        locs, warnings = _normalize_lsp_locations(raw, self._root)
        warnings = [*readiness_warnings, *warnings]
        total = len(locs)
        return CodeImplementationsResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            implementations=locs[:max_results],
            truncated=total > max_results,
            warnings=warnings,
        )

    async def hover(
        self, rel: str, pub_line: int, pub_char: int, snapshot_id: str
    ) -> CodeHoverResult:
        lsp_line, lsp_char = self._lsp_line_char(rel, pub_line, pub_char)
        raw = await self._client.request_hover(self._uri(rel), lsp_line, lsp_char)
        if raw is None:
            return CodeHoverResult(
                snapshot_id=snapshot_id,
                provider=ProviderKind.LSP,
                precision=Precision.EXACT,
                captured_at=_now_iso(),
            )
        content_str: str | None = None
        format_str: Literal["markdown", "plaintext"] | None = None
        hover_range: CodeRange | None = None

        contents = raw.contents
        if isinstance(contents, lsp.MarkupContent):
            content_str = contents.value
            format_str = "markdown" if contents.kind == lsp.MarkupKind.Markdown else "plaintext"
        elif isinstance(contents, str):
            content_str = contents
            format_str = "plaintext"
        elif isinstance(contents, lsp.MarkedStringWithLanguage):
            content_str = contents.value
            format_str = "plaintext"
        elif isinstance(contents, Sequence):
            parts = []
            for item in contents:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, lsp.MarkedStringWithLanguage):
                    parts.append(item.value)
            content_str = "\n\n".join(parts)
            format_str = "plaintext"

        if raw.range is not None:
            start_text = self._line_text(rel, raw.range.start.line)
            end_text = self._line_text(rel, raw.range.end.line)
            hover_range = public_range_from_lsp(start_text, end_text, raw.range)

        return CodeHoverResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            content=content_str,
            format=format_str,
            range=hover_range,
        )

    async def document_symbols(
        self,
        rel: str,
        query: str,
        kinds: Sequence[SymbolKind],
        max_results: int,
        snapshot_id: str,
    ) -> CodeSymbolSearchResult:
        raw = await self._client.request_document_symbols(self._uri(rel))
        if raw is None:
            return CodeSymbolSearchResult(
                snapshot_id=snapshot_id,
                provider=ProviderKind.LSP,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["Document symbol request returned no data"],
            )

        symbols: list[SymbolInfo] = []
        query_lower = query.casefold()

        def append_document_symbol(item: lsp.DocumentSymbol, container: str | None) -> None:
            kind = _lsp_symbol_kind(item.kind)
            if (not query_lower or query_lower in item.name.casefold()) and (
                not kinds or kind in kinds
            ):
                start_text = self._line_text(rel, item.selection_range.start.line)
                end_text = self._line_text(rel, item.selection_range.end.line)
                symbols.append(
                    SymbolInfo(
                        name=item.name,
                        kind=kind,
                        location=CodeLocation(
                            path=rel,
                            range=public_range_from_lsp(start_text, end_text, item.selection_range),
                        ),
                        container=container,
                    )
                )
            for child in item.children or []:
                append_document_symbol(child, item.name)

        for item in raw:
            if isinstance(item, lsp.DocumentSymbol):
                append_document_symbol(item, None)
                continue
            if query_lower and query_lower not in item.name.casefold():
                continue
            kind = _lsp_symbol_kind(item.kind)
            if kinds and kind not in kinds:
                continue
            location = _lsp_location_to_code(item.location, self._root)
            if location is None or location.path != rel:
                continue
            symbols.append(
                SymbolInfo(
                    name=item.name,
                    kind=kind,
                    location=location,
                    container=item.container_name,
                )
            )

        total = len(symbols)
        return CodeSymbolSearchResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            symbols=symbols[:max_results],
            total_matches=total,
            truncated=total > max_results,
        )

    async def workspace_symbols(
        self,
        query: str,
        kinds: Sequence[SymbolKind],
        max_results: int,
        snapshot_id: str,
    ) -> CodeSymbolSearchResult:
        readiness_warnings = await self._workspace_ready_warnings()
        raw = await self._client.request_workspace_symbols(query)
        if raw is None:
            return CodeSymbolSearchResult(
                snapshot_id=snapshot_id,
                provider=ProviderKind.LSP,
                precision=Precision.EXACT,
                captured_at=_now_iso(),
                warnings=["Workspace symbol request returned no data"],
            )
        symbols: list[SymbolInfo] = []
        external = 0
        for item in raw:
            if isinstance(item, (lsp.SymbolInformation, lsp.WorkspaceSymbol)):
                loc = getattr(item, "location", None)
                if loc is None:
                    continue
                if isinstance(loc, lsp.Location):
                    rel = uri_to_project_relative(loc.uri, self._root)
                    if rel is None:
                        external += 1
                        continue
                    start_text = _read_line(self._root / rel, loc.range.start.line)
                    end_text = _read_line(self._root / rel, loc.range.end.line)
                    code_loc = CodeLocation(
                        path=rel,
                        range=public_range_from_lsp(start_text, end_text, loc.range),
                    )
                else:
                    # WorkspaceSymbol with LocationLink — use uri only
                    rel = uri_to_project_relative(getattr(loc, "uri", ""), self._root)
                    if rel is None:
                        external += 1
                        continue
                    code_loc = CodeLocation(path=rel)
                kind = _lsp_symbol_kind(item.kind)
                if kinds and kind not in kinds:
                    continue
                symbols.append(
                    SymbolInfo(
                        name=item.name,
                        kind=kind,
                        location=code_loc,
                        container=getattr(item, "container_name", None),
                    )
                )

        warnings = list(readiness_warnings)
        if external:
            warnings.append(f"{external} external symbol(s) omitted")
        total = len(symbols)
        return CodeSymbolSearchResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            symbols=symbols[:max_results],
            total_matches=total,
            truncated=total > max_results,
            warnings=warnings,
        )

    async def diagnostics(
        self,
        rel: str,
        severity_min: DiagnosticSeverity,
        max_results: int,
        snapshot_id: str,
    ) -> CodeDiagnosticsResult:
        readiness_warnings = await self._workspace_ready_warnings()
        raw_diags, state_value = await self._client.diagnostic_snapshot(self._uri(rel))
        severity_threshold = _SEVERITY_ORDER[severity_min]
        diags: list[CodeDiagnostic] = []

        for d in raw_diags:
            sev = _lsp_severity(d.severity)
            if _SEVERITY_ORDER.get(sev, 99) > severity_threshold:
                continue
            start_text = _read_line(self._root / rel, d.range.start.line)
            end_text = _read_line(self._root / rel, d.range.end.line)
            code_range = public_range_from_lsp(start_text, end_text, d.range)
            code_str = None
            if d.code is not None:
                code_str = str(d.code) if not isinstance(d.code, str) else d.code
            diags.append(
                CodeDiagnostic(
                    path=rel,
                    range=code_range,
                    severity=sev,
                    code=code_str,
                    message=d.message[:4096],
                    source=d.source,
                )
            )

        total = len(diags)
        state = DiagnosticState(state_value)
        warnings = list(readiness_warnings)
        if state != DiagnosticState.READY:
            warnings.append(
                "Diagnostics are pending, partial, stale, or unavailable; "
                "an empty list does not prove a clean build"
            )

        return CodeDiagnosticsResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            diagnostics=diags[:max_results],
            state=state,
            truncated=total > max_results,
            warnings=warnings,
        )

    async def call_hierarchy(
        self,
        rel: str,
        pub_line: int,
        pub_char: int,
        direction: str,
        max_depth: int,
        max_nodes: int,
        snapshot_id: str,
    ) -> CodeCallHierarchyResult:
        readiness_warnings = await self._workspace_ready_warnings()
        lsp_line, lsp_char = self._lsp_line_char(rel, pub_line, pub_char)
        prepared = await self._client.request_prepare_call_hierarchy(
            self._uri(rel), lsp_line, lsp_char
        )
        if not prepared:
            return CodeCallHierarchyResult(
                snapshot_id=snapshot_id,
                provider=ProviderKind.LSP,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["No call hierarchy item found at this position"],
            )
        root_item = prepared[0]

        def key(item: lsp.CallHierarchyItem) -> tuple[object, ...]:
            return (
                item.uri,
                item.name,
                item.range.start.line,
                item.range.start.character,
                item.range.end.line,
                item.range.end.character,
            )

        def convert(
            item: lsp.CallHierarchyItem,
            depth: int,
            ranges: Sequence[lsp.Range] = (),
        ) -> CallItem | None:
            item_rel = uri_to_project_relative(item.uri, self._root)
            if item_rel is None:
                return None
            start_text = self._line_text(item_rel, item.selection_range.start.line)
            end_text = self._line_text(item_rel, item.selection_range.end.line)
            converted_ranges: list[CodeRange] = []
            for source_range in ranges[:10]:
                range_start = self._line_text(item_rel, source_range.start.line)
                range_end = self._line_text(item_rel, source_range.end.line)
                converted_ranges.append(public_range_from_lsp(range_start, range_end, source_range))
            return CallItem(
                name=item.name,
                kind=_lsp_symbol_kind(item.kind),
                location=CodeLocation(
                    path=item_rel,
                    range=public_range_from_lsp(start_text, end_text, item.selection_range),
                ),
                ranges=converted_ranges,
                depth=depth,
            )

        root = convert(root_item, 0) or CallItem(
            name=root_item.name,
            kind=_lsp_symbol_kind(root_item.kind),
            location=CodeLocation(path=rel),
            depth=0,
        )
        total_nodes = 1
        truncated = False

        async def walk(incoming: bool) -> list[CallItem]:
            nonlocal total_nodes, truncated
            found: list[CallItem] = []
            queue: deque[tuple[lsp.CallHierarchyItem, int]] = deque([(root_item, 0)])
            seen = {key(root_item)}
            while queue and total_nodes < max_nodes:
                current, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                relations = (
                    await self._client.request_incoming_calls(current)
                    if incoming
                    else await self._client.request_outgoing_calls(current)
                )
                for relation in relations or []:
                    if incoming:
                        assert isinstance(relation, lsp.CallHierarchyIncomingCall)
                        child = relation.from_
                        ranges = relation.from_ranges
                    else:
                        assert isinstance(relation, lsp.CallHierarchyOutgoingCall)
                        child = relation.to
                        ranges = ()
                    child_key = key(child)
                    if child_key in seen:
                        continue
                    if total_nodes >= max_nodes:
                        truncated = True
                        break
                    converted = convert(child, depth + 1, ranges)
                    if converted is None:
                        continue
                    seen.add(child_key)
                    found.append(converted)
                    total_nodes += 1
                    queue.append((child, depth + 1))
                if total_nodes >= max_nodes and queue:
                    truncated = True
            return found

        incoming_calls: list[CallItem] = []
        outgoing_calls: list[CallItem] = []
        if direction in ("incoming", "both"):
            incoming_calls = await walk(True)
        if direction in ("outgoing", "both") and total_nodes < max_nodes:
            outgoing_calls = await walk(False)
        elif direction in ("outgoing", "both") and total_nodes >= max_nodes:
            truncated = True

        return CodeCallHierarchyResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            root=root,
            incoming_calls=incoming_calls,
            outgoing_calls=outgoing_calls,
            truncated=truncated,
            warnings=readiness_warnings,
        )

    async def type_hierarchy(
        self,
        rel: str,
        pub_line: int,
        pub_char: int,
        direction: str,
        max_depth: int,
        max_nodes: int,
        snapshot_id: str,
    ) -> CodeTypeHierarchyResult:
        readiness_warnings = await self._workspace_ready_warnings()
        lsp_line, lsp_char = self._lsp_line_char(rel, pub_line, pub_char)
        prepared = await self._client.request_prepare_type_hierarchy(
            self._uri(rel), lsp_line, lsp_char
        )
        if not prepared:
            return CodeTypeHierarchyResult(
                snapshot_id=snapshot_id,
                provider=ProviderKind.LSP,
                precision=Precision.UNAVAILABLE,
                captured_at=_now_iso(),
                warnings=["No type hierarchy item found at this position"],
            )
        root_item = prepared[0]

        def key(item: lsp.TypeHierarchyItem) -> tuple[object, ...]:
            return (
                item.uri,
                item.name,
                item.range.start.line,
                item.range.start.character,
                item.range.end.line,
                item.range.end.character,
            )

        def convert(item: lsp.TypeHierarchyItem, depth: int) -> TypeItem | None:
            item_rel = uri_to_project_relative(item.uri, self._root)
            if item_rel is None:
                return None
            start_text = self._line_text(item_rel, item.selection_range.start.line)
            end_text = self._line_text(item_rel, item.selection_range.end.line)
            return TypeItem(
                name=item.name,
                kind=_lsp_symbol_kind(item.kind),
                location=CodeLocation(
                    path=item_rel,
                    range=public_range_from_lsp(start_text, end_text, item.selection_range),
                ),
                depth=depth,
            )

        root = convert(root_item, 0) or TypeItem(
            name=root_item.name,
            kind=_lsp_symbol_kind(root_item.kind),
            location=CodeLocation(path=rel),
            depth=0,
        )
        total_nodes = 1
        truncated = False

        async def walk(supertypes: bool) -> list[TypeItem]:
            nonlocal total_nodes, truncated
            found: list[TypeItem] = []
            queue: deque[tuple[lsp.TypeHierarchyItem, int]] = deque([(root_item, 0)])
            seen = {key(root_item)}
            while queue and total_nodes < max_nodes:
                current, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                children = (
                    await self._client.request_supertypes(current)
                    if supertypes
                    else await self._client.request_subtypes(current)
                )
                for child in children or []:
                    child_key = key(child)
                    if child_key in seen:
                        continue
                    if total_nodes >= max_nodes:
                        truncated = True
                        break
                    converted = convert(child, depth + 1)
                    if converted is None:
                        continue
                    seen.add(child_key)
                    found.append(converted)
                    total_nodes += 1
                    queue.append((child, depth + 1))
                if total_nodes >= max_nodes and queue:
                    truncated = True
            return found

        supertypes: list[TypeItem] = []
        subtypes: list[TypeItem] = []
        if direction in ("supertypes", "both"):
            supertypes = await walk(True)
        if direction in ("subtypes", "both") and total_nodes < max_nodes:
            subtypes = await walk(False)
        elif direction in ("subtypes", "both") and total_nodes >= max_nodes:
            truncated = True

        return CodeTypeHierarchyResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=_now_iso(),
            root=root,
            supertypes=supertypes,
            subtypes=subtypes,
            truncated=truncated,
            warnings=readiness_warnings,
        )
