"""Local-only file target resolution; remote callers cannot register roots."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .device_paths import WindowsReadScope
from .errors import AgentError
from .models import Project
from .paths import ProjectPathResolver, root_fingerprint


@dataclass(frozen=True)
class FileTarget:
    project: Project
    inside_project: bool
    scope_path: str | None = None
    scope_identity: str | None = None
    pinned_scope: WindowsReadScope | None = None


@contextmanager
def resolve_file_target(project: Project, scope_path: str | None) -> Iterator[FileTarget]:
    """Pin explicit external roots through consent and IO; never persist a project."""
    if not project.enabled:
        raise AgentError("project_disabled", "The requested project is disabled")
    ProjectPathResolver().verify_project(project)
    if scope_path is None:
        yield FileTarget(project, True)
        return
    with WindowsReadScope(scope_path) as scope:
        root = Path(scope.scope_path)
        target = replace(project, root=root, root_fingerprint=root_fingerprint(root))
        yield FileTarget(
            target,
            root.is_relative_to(project.root),
            scope.scope_path,
            scope.identity,
            scope,
        )
