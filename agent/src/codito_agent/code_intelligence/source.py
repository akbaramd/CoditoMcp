"""Snapshot-bound source access shared by LSP, graph and context aggregation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..errors import AgentError
from ..paths import ProjectPathResolver
from .snapshot import _safe_project
from .types import WorkspaceSnapshot

_ACTIVE: ContextVar[WorkspaceSnapshot | None] = ContextVar("codito_code_snapshot", default=None)


@contextmanager
def source_snapshot(snapshot: WorkspaceSnapshot) -> Iterator[None]:
    token = _ACTIVE.set(snapshot)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def current_snapshot() -> WorkspaceSnapshot | None:
    return _ACTIVE.get()


def read_source_bytes(
    root: Path, relative: str, snapshot: WorkspaceSnapshot | None = None
) -> bytes:
    snapshot = snapshot or _ACTIVE.get()
    if snapshot is not None:
        if snapshot.project_root != root or relative not in snapshot.file_states:
            raise AgentError("unsafe_path", "Source is outside the verified analysis snapshot")
        identity = snapshot.root_identity
    else:
        identity = None
    project = _safe_project(root, identity)
    with ProjectPathResolver().open_read(project, relative) as stream:
        data = stream.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise AgentError("file_too_large", "Source exceeds the analysis size limit")
    if (
        snapshot is not None
        and hashlib.sha256(data).hexdigest() != snapshot.file_states[relative].content_hash
    ):
        raise AgentError("path_race", "Source changed after the analysis snapshot", retryable=True)
    return data


def read_source_text(root: Path, relative: str, snapshot: WorkspaceSnapshot | None = None) -> str:
    data = read_source_bytes(root, relative, snapshot)
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        return data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise AgentError(
            "unsupported_encoding", "Code analysis requires UTF-8 or BOM-marked UTF-16"
        ) from exc


def source_line(root: Path, relative: str, zero_based_line: int) -> str:
    lines = read_source_text(root, relative).split("\n")
    if zero_based_line < 0 or zero_based_line >= len(lines):
        raise AgentError("invalid_request", "Code location is outside the current source document")
    return lines[zero_based_line]
