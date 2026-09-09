"""Wire and result models for the project_code MCP tool (code intelligence).

Public coordinates: 1-based lines, 1-based Unicode-scalar columns, end-exclusive.
The agent converts to/from LSP UTF-16 offsets including astral Unicode and CRLF.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from .types import CoditoModel, OpaqueId, RelativePath

# ---------------------------------------------------------------------------
# Shared coordinate and provenance types
# ---------------------------------------------------------------------------


class Precision(StrEnum):
    EXACT = "exact"
    STRUCTURAL = "structural"
    HEURISTIC = "heuristic"
    UNAVAILABLE = "unavailable"


class ProviderKind(StrEnum):
    LSP = "lsp"
    GRAPH = "graph"
    MANIFEST = "manifest"
    NONE = "none"


class DiagnosticSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    HINT = "hint"


class SymbolKind(StrEnum):
    FILE = "file"
    MODULE = "module"
    NAMESPACE = "namespace"
    PACKAGE = "package"
    CLASS = "class"
    METHOD = "method"
    PROPERTY = "property"
    FIELD = "field"
    CONSTRUCTOR = "constructor"
    ENUM = "enum"
    INTERFACE = "interface"
    FUNCTION = "function"
    VARIABLE = "variable"
    CONSTANT = "constant"
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"
    KEY = "key"
    NULL = "null"
    ENUM_MEMBER = "enum_member"
    STRUCT = "struct"
    EVENT = "event"
    OPERATOR = "operator"
    TYPE_PARAMETER = "type_parameter"
    UNKNOWN = "unknown"


class WorkspaceReadyState(StrEnum):
    READY = "ready"
    INDEXING = "indexing"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


class DiagnosticState(StrEnum):
    READY = "ready"
    STALE = "stale"
    PARTIAL = "partial"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


class ReindexScope(StrEnum):
    INCREMENTAL = "incremental"
    FULL = "full"


class HierarchyDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    BOTH = "both"


class TypeHierarchyDirection(StrEnum):
    SUPERTYPES = "supertypes"
    SUBTYPES = "subtypes"
    BOTH = "both"


# 1-based, Unicode scalar
PositiveLine = Annotated[int, Field(ge=1, le=10_000_000)]
PositiveCharacter = Annotated[int, Field(ge=1, le=100_000)]
SnapshotId = Annotated[str, Field(min_length=1, max_length=128)]
SymbolRefId = Annotated[str, Field(min_length=16, max_length=512)]


class CodePosition(CoditoModel):
    """1-based line and Unicode-scalar column (not UTF-16)."""

    line: PositiveLine
    character: PositiveCharacter


class CodeRange(CoditoModel):
    """Half-open [start, end) range. Both endpoints are 1-based Unicode scalars."""

    start: CodePosition
    end: CodePosition


class CodeLocation(CoditoModel):
    path: RelativePath
    range: CodeRange | None = None


class CodeDiagnostic(CoditoModel):
    path: RelativePath
    range: CodeRange
    severity: DiagnosticSeverity
    code: str | None = Field(default=None, max_length=256)
    message: str = Field(min_length=1, max_length=4096)
    source: str | None = Field(default=None, max_length=256)


class SymbolInfo(CoditoModel):
    name: str = Field(min_length=1, max_length=512)
    kind: SymbolKind = SymbolKind.UNKNOWN
    location: CodeLocation
    container: str | None = Field(default=None, max_length=512)


class CallItem(CoditoModel):
    name: str = Field(min_length=1, max_length=512)
    kind: SymbolKind = SymbolKind.UNKNOWN
    location: CodeLocation
    ranges: list[CodeRange] = Field(default_factory=list, max_length=100)
    depth: int = Field(default=0, ge=0, le=10)


class TypeItem(CoditoModel):
    depth: int = Field(default=0, ge=0, le=10)
    name: str = Field(min_length=1, max_length=512)
    kind: SymbolKind = SymbolKind.UNKNOWN
    location: CodeLocation


class ImpactNode(CoditoModel):
    path: RelativePath
    reason: str = Field(max_length=1024)
    confidence: Precision = Precision.STRUCTURAL


class PackageInfo(CoditoModel):
    name: str = Field(min_length=1, max_length=512)
    path: RelativePath
    file_count: int = Field(ge=0)
    dependencies: list[str] = Field(default_factory=list, max_length=500)


class ManifestInfo(CoditoModel):
    path: RelativePath
    kind: str = Field(min_length=1, max_length=64)


class LanguageSummary(CoditoModel):
    language: str = Field(min_length=1, max_length=64)
    file_count: int = Field(ge=0)
    extensions: list[str] = Field(default_factory=list, max_length=32)


class DependencyInfo(CoditoModel):
    name: str = Field(min_length=1, max_length=512)
    version: str | None = Field(default=None, max_length=256)
    path: RelativePath | None = None
    kind: str = Field(min_length=1, max_length=64)
    direction: Literal["direct", "transitive", "dev"] = "direct"


class TestRelation(CoditoModel):
    path: RelativePath
    name: str | None = Field(default=None, max_length=512)
    relationship: Literal["exact", "structural_call", "heuristic_name", "heuristic_path"] = (
        "heuristic_path"
    )


class CapabilityInfo(CoditoModel):
    definition: bool = False
    references: bool = False
    implementations: bool = False
    hover: bool = False
    diagnostics: bool = False
    document_symbols: bool = False
    workspace_symbols: bool = False
    call_hierarchy: bool = False
    type_hierarchy: bool = False


class ContextSection(CoditoModel):
    available: bool
    precision: Precision = Precision.UNAVAILABLE
    provider: ProviderKind = ProviderKind.NONE


class ContextSections(CoditoModel):
    definition: ContextSection
    hover: ContextSection
    references: ContextSection
    diagnostics: ContextSection
    source_lines: ContextSection


# ---------------------------------------------------------------------------
# Common result base
# ---------------------------------------------------------------------------


class CodeResultBase(CoditoModel):
    snapshot_id: SnapshotId
    provider: ProviderKind = ProviderKind.NONE
    precision: Precision = Precision.UNAVAILABLE
    captured_at: str = Field(min_length=1, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=16)
    freshness: Literal["verified", "stale", "unknown"] = "unknown"
    verified_at: str | None = Field(default=None, max_length=64)
    complete: bool = False


# ---------------------------------------------------------------------------
# Input models (wire level; facade adds per-tool input models)
# ---------------------------------------------------------------------------


class CodeIntelligenceStatusInput(CoditoModel):
    operation: Literal["code_intelligence_status"] = "code_intelligence_status"
    project_id: OpaqueId
    purpose: str = Field(default="Query code intelligence status", min_length=1, max_length=1000)


class CodeWorkspaceSummaryInput(CoditoModel):
    operation: Literal["code_workspace_summary"] = "code_workspace_summary"
    project_id: OpaqueId
    purpose: str = Field(default="Summarise workspace", min_length=1, max_length=1000)


class CodeSymbolSearchInput(CoditoModel):
    operation: Literal["code_symbol_search"] = "code_symbol_search"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    query: str = Field(min_length=1, max_length=512)
    scope: Literal["workspace", "document"] = "workspace"
    path: RelativePath | None = None

    @model_validator(mode="after")
    def document_path_required(self) -> CodeSymbolSearchInput:
        if self.scope == "document" and self.path is None:
            raise ValueError("document symbol search requires path")
        return self

    kinds: list[SymbolKind] = Field(default_factory=list, max_length=27)
    max_results: int = Field(default=50, ge=1, le=500)


class CodeDefinitionInput(CoditoModel):
    operation: Literal["code_definition"] = "code_definition"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    line: PositiveLine
    character: PositiveCharacter


class CodeReferencesInput(CoditoModel):
    operation: Literal["code_references"] = "code_references"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    line: PositiveLine
    character: PositiveCharacter
    include_declaration: bool = True
    max_results: int = Field(default=100, ge=1, le=1000)


class CodeImplementationsInput(CoditoModel):
    operation: Literal["code_implementations"] = "code_implementations"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    line: PositiveLine
    character: PositiveCharacter
    max_results: int = Field(default=100, ge=1, le=500)


class CodeDiagnosticsInput(CoditoModel):
    operation: Literal["code_diagnostics"] = "code_diagnostics"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    severity_min: DiagnosticSeverity = DiagnosticSeverity.HINT
    max_results: int = Field(default=200, ge=1, le=1000)


class CodeHoverInput(CoditoModel):
    operation: Literal["code_hover"] = "code_hover"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    line: PositiveLine
    character: PositiveCharacter


class CodeContextInput(CoditoModel):
    operation: Literal["code_context"] = "code_context"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    line: PositiveLine
    character: PositiveCharacter
    source_context_lines: int = Field(default=5, ge=0, le=50)
    include_references: bool = True
    include_diagnostics: bool = True


class CodeCallHierarchyInput(CoditoModel):
    operation: Literal["code_call_hierarchy"] = "code_call_hierarchy"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    line: PositiveLine
    character: PositiveCharacter
    direction: HierarchyDirection = HierarchyDirection.BOTH
    max_depth: int = Field(default=3, ge=1, le=10)
    max_nodes: int = Field(default=50, ge=1, le=200)


class CodeTypeHierarchyInput(CoditoModel):
    operation: Literal["code_type_hierarchy"] = "code_type_hierarchy"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    line: PositiveLine
    character: PositiveCharacter
    direction: TypeHierarchyDirection = TypeHierarchyDirection.BOTH
    max_depth: int = Field(default=3, ge=1, le=10)
    max_nodes: int = Field(default=50, ge=1, le=200)


class CodeImpactInput(CoditoModel):
    operation: Literal["code_impact"] = "code_impact"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    line: PositiveLine | None = None
    character: PositiveCharacter | None = None
    max_nodes: int = Field(default=50, ge=1, le=500)


class CodeArchitectureInput(CoditoModel):
    operation: Literal["code_architecture"] = "code_architecture"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    max_packages: int = Field(default=100, ge=1, le=500)


class CodeDependenciesInput(CoditoModel):
    operation: Literal["code_dependencies"] = "code_dependencies"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath | None = None
    direction: Literal["all", "direct", "transitive"] = "direct"
    max_results: int = Field(default=100, ge=1, le=500)


class CodeRelatedTestsInput(CoditoModel):
    operation: Literal["code_related_tests"] = "code_related_tests"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    path: RelativePath
    max_results: int = Field(default=50, ge=1, le=200)


class CodeReindexInput(CoditoModel):
    operation: Literal["code_reindex"] = "code_reindex"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1000)
    scope: ReindexScope = ReindexScope.INCREMENTAL
    timeout_seconds: int = Field(default=30, ge=5, le=30)


ProjectCodeInput = (
    CodeIntelligenceStatusInput
    | CodeWorkspaceSummaryInput
    | CodeSymbolSearchInput
    | CodeDefinitionInput
    | CodeReferencesInput
    | CodeImplementationsInput
    | CodeDiagnosticsInput
    | CodeHoverInput
    | CodeContextInput
    | CodeCallHierarchyInput
    | CodeTypeHierarchyInput
    | CodeImpactInput
    | CodeArchitectureInput
    | CodeDependenciesInput
    | CodeRelatedTestsInput
    | CodeReindexInput
)

# ---------------------------------------------------------------------------
# Result models (wire level)
# ---------------------------------------------------------------------------


class CodeIntelligenceStatusResult(CodeResultBase):
    operation: Literal["code_intelligence_status"] = "code_intelligence_status"
    status: WorkspaceReadyState
    lsp_server: str | None = Field(default=None, max_length=256)
    capabilities: CapabilityInfo = Field(default_factory=CapabilityInfo)
    indexed_files: int = Field(default=0, ge=0)
    setup_requirements: list[str] = Field(default_factory=list, max_length=32)


class CodeWorkspaceSummaryResult(CodeResultBase):
    operation: Literal["code_workspace_summary"] = "code_workspace_summary"
    languages: list[LanguageSummary] = Field(default_factory=list, max_length=64)
    manifests: list[ManifestInfo] = Field(default_factory=list, max_length=128)
    frameworks: list[str] = Field(default_factory=list, max_length=64)
    build_roots: list[RelativePath] = Field(default_factory=list, max_length=32)
    total_files: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)


class CodeSymbolSearchResult(CodeResultBase):
    operation: Literal["code_symbol_search"] = "code_symbol_search"
    symbols: list[SymbolInfo] = Field(default_factory=list, max_length=500)
    total_matches: int = Field(default=0, ge=0)
    truncated: bool = False


class CodeDefinitionResult(CodeResultBase):
    operation: Literal["code_definition"] = "code_definition"
    definitions: list[CodeLocation] = Field(default_factory=list, max_length=32)


class CodeReferencesResult(CodeResultBase):
    operation: Literal["code_references"] = "code_references"
    references: list[CodeLocation] = Field(default_factory=list, max_length=1000)
    truncated: bool = False
    total_matches: int = Field(default=0, ge=0)


class CodeImplementationsResult(CodeResultBase):
    operation: Literal["code_implementations"] = "code_implementations"
    implementations: list[CodeLocation] = Field(default_factory=list, max_length=500)
    truncated: bool = False


class CodeDiagnosticsResult(CodeResultBase):
    operation: Literal["code_diagnostics"] = "code_diagnostics"
    diagnostics: list[CodeDiagnostic] = Field(default_factory=list, max_length=1000)
    state: DiagnosticState = DiagnosticState.UNAVAILABLE
    truncated: bool = False


class CodeHoverResult(CodeResultBase):
    operation: Literal["code_hover"] = "code_hover"
    content: str | None = Field(default=None, max_length=32768)
    format: Literal["markdown", "plaintext"] | None = None
    range: CodeRange | None = None


class CodeContextResult(CodeResultBase):
    operation: Literal["code_context"] = "code_context"
    definition: CodeDefinitionResult | None = None
    hover: CodeHoverResult | None = None
    references: CodeReferencesResult | None = None
    diagnostics: CodeDiagnosticsResult | None = None
    source_lines: str | None = Field(default=None, max_length=65536)
    source_start_line: int | None = Field(default=None, ge=1)
    sections: ContextSections | None = None


class CodeCallHierarchyResult(CodeResultBase):
    operation: Literal["code_call_hierarchy"] = "code_call_hierarchy"
    root: CallItem | None = None
    incoming_calls: list[CallItem] = Field(default_factory=list, max_length=200)
    outgoing_calls: list[CallItem] = Field(default_factory=list, max_length=200)
    truncated: bool = False


class CodeTypeHierarchyResult(CodeResultBase):
    operation: Literal["code_type_hierarchy"] = "code_type_hierarchy"
    root: TypeItem | None = None
    supertypes: list[TypeItem] = Field(default_factory=list, max_length=100)
    subtypes: list[TypeItem] = Field(default_factory=list, max_length=200)
    truncated: bool = False


class CodeImpactResult(CodeResultBase):
    operation: Literal["code_impact"] = "code_impact"
    dependents: list[ImpactNode] = Field(default_factory=list, max_length=500)
    truncated: bool = False


class CodeArchitectureResult(CodeResultBase):
    operation: Literal["code_architecture"] = "code_architecture"
    packages: list[PackageInfo] = Field(default_factory=list, max_length=500)
    manifests: list[ManifestInfo] = Field(default_factory=list, max_length=128)
    truncated: bool = False


class CodeDependenciesResult(CodeResultBase):
    operation: Literal["code_dependencies"] = "code_dependencies"
    dependencies: list[DependencyInfo] = Field(default_factory=list, max_length=500)
    truncated: bool = False


class CodeRelatedTestsResult(CodeResultBase):
    operation: Literal["code_related_tests"] = "code_related_tests"
    tests: list[TestRelation] = Field(default_factory=list, max_length=200)
    truncated: bool = False


class CodeReindexResult(CodeResultBase):
    operation: Literal["code_reindex"] = "code_reindex"
    completed: bool
    generation: SnapshotId
    files_processed: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list, max_length=32)
    timed_out: bool = False


ProjectCodeResult = (
    CodeIntelligenceStatusResult
    | CodeWorkspaceSummaryResult
    | CodeSymbolSearchResult
    | CodeDefinitionResult
    | CodeReferencesResult
    | CodeImplementationsResult
    | CodeDiagnosticsResult
    | CodeHoverResult
    | CodeContextResult
    | CodeCallHierarchyResult
    | CodeTypeHierarchyResult
    | CodeImpactResult
    | CodeArchitectureResult
    | CodeDependenciesResult
    | CodeRelatedTestsResult
    | CodeReindexResult
)

_OPERATION_INPUT_MAP: dict[str, type[CoditoModel]] = {
    "code_intelligence_status": CodeIntelligenceStatusInput,
    "code_workspace_summary": CodeWorkspaceSummaryInput,
    "code_symbol_search": CodeSymbolSearchInput,
    "code_definition": CodeDefinitionInput,
    "code_references": CodeReferencesInput,
    "code_implementations": CodeImplementationsInput,
    "code_diagnostics": CodeDiagnosticsInput,
    "code_hover": CodeHoverInput,
    "code_context": CodeContextInput,
    "code_call_hierarchy": CodeCallHierarchyInput,
    "code_type_hierarchy": CodeTypeHierarchyInput,
    "code_impact": CodeImpactInput,
    "code_architecture": CodeArchitectureInput,
    "code_dependencies": CodeDependenciesInput,
    "code_related_tests": CodeRelatedTestsInput,
    "code_reindex": CodeReindexInput,
}

_OPERATION_RESULT_MAP: dict[str, type[CoditoModel]] = {
    "code_intelligence_status": CodeIntelligenceStatusResult,
    "code_workspace_summary": CodeWorkspaceSummaryResult,
    "code_symbol_search": CodeSymbolSearchResult,
    "code_definition": CodeDefinitionResult,
    "code_references": CodeReferencesResult,
    "code_implementations": CodeImplementationsResult,
    "code_diagnostics": CodeDiagnosticsResult,
    "code_hover": CodeHoverResult,
    "code_context": CodeContextResult,
    "code_call_hierarchy": CodeCallHierarchyResult,
    "code_type_hierarchy": CodeTypeHierarchyResult,
    "code_impact": CodeImpactResult,
    "code_architecture": CodeArchitectureResult,
    "code_dependencies": CodeDependenciesResult,
    "code_related_tests": CodeRelatedTestsResult,
    "code_reindex": CodeReindexResult,
}


_INPUT_ADAPTER: TypeAdapter[ProjectCodeInput] = TypeAdapter(ProjectCodeInput)
_RESULT_ADAPTER: TypeAdapter[ProjectCodeResult] = TypeAdapter(ProjectCodeResult)


def validate_project_code(value: Any) -> ProjectCodeInput:
    return _INPUT_ADAPTER.validate_python(value)


def validate_project_code_result(value: Any) -> ProjectCodeResult:
    return _RESULT_ADAPTER.validate_python(value)
