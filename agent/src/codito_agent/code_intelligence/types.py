"""Internal types for the code intelligence subsystem.

These types are never exposed in public MCP output; public contracts live in
codito_protocol.code. Internal types may reference LSP-specific concepts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class WorkspaceStatus(StrEnum):
    NOT_STARTED = "not_started"
    INITIALIZING = "initializing"
    READY = "ready"
    INDEXING = "indexing"
    STALE = "stale"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """Represents a single file's state in the workspace snapshot."""

    mtime_ns: int
    size: int
    content_hash: str  # SHA-256 hex digest

    def matches(self, other: SnapshotEntry) -> bool:
        if self.mtime_ns != other.mtime_ns or self.size != other.size:
            return False
        # Same mtime+size: compare content hash to catch silent overwrites
        return self.content_hash == other.content_hash


@dataclass
class WorkspaceSnapshot:
    """Immutable snapshot generation identifier + file state map."""

    generation: str  # Unique per-refresh ID
    project_root: Path
    file_states: dict[str, SnapshotEntry] = field(default_factory=dict)
    root_identity: str | None = None

    @classmethod
    def empty(cls, project_root: Path) -> WorkspaceSnapshot:
        return cls(
            generation=_new_generation(),
            project_root=project_root,
            file_states={},
        )


def _new_generation() -> str:
    import secrets

    return secrets.token_hex(12)


@dataclass
class CapabilitySet:
    """Negotiated LSP server capabilities."""

    definition: bool = False
    references: bool = False
    implementations: bool = False
    hover: bool = False
    diagnostics_pull: bool = False
    diagnostics_push: bool = False
    document_symbols: bool = False
    workspace_symbols: bool = False
    call_hierarchy: bool = False
    type_hierarchy: bool = False

    @classmethod
    def from_lsp_capabilities(cls, caps: Any) -> CapabilitySet:
        if caps is None:
            return cls()
        result = cls()
        result.definition = _cap(caps, "definition_provider")
        result.references = _cap(caps, "references_provider")
        result.implementations = _cap(caps, "implementation_provider")
        result.hover = _cap(caps, "hover_provider")
        result.document_symbols = _cap(caps, "document_symbol_provider")
        result.workspace_symbols = _cap(caps, "workspace_symbol_provider")
        result.call_hierarchy = _cap(caps, "call_hierarchy_provider")
        result.type_hierarchy = _cap(caps, "type_hierarchy_provider")
        diag = getattr(caps, "diagnostic_provider", None)
        result.diagnostics_pull = diag is not None
        return result


def _cap(caps: Any, attr: str) -> bool:
    value = getattr(caps, attr, None)
    if value is None or value is False:
        return False
    return True


# ---------------------------------------------------------------------------
# Ignored directory patterns (project-relative)
# ---------------------------------------------------------------------------

EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".codebase-memory",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".next",
        "dist",
        "build",
        "bin",
        "obj",
        "out",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".uv-cache",
        ".pip-audit-cache",
        ".hypothesis",
        ".hypothesis-task",
        ".mypy-task-cache",
        "coverage",
        ".coverage",
        "htmlcov",
        ".eggs",
        "*.egg-info",
        ".tox",
        ".nox",
        "target",  # Rust/Java
        ".gradle",
        ".idea",
        ".vs",
        ".vscode",
        "artifacts",
        "installer-output",
    }
)

# Maximum snapshot bounds
MAX_SNAPSHOT_FILES = 50_000
MAX_SNAPSHOT_BYTES = 500 * 1024 * 1024  # 500 MB total source
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB per file


def hash_file_content(path: Path) -> str:
    """Return SHA-256 hex digest of file content. Raises OSError on failure."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
