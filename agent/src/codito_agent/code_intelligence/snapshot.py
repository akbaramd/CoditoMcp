"""Bounded, content-hashed snapshots using the existing approved-path resolver.

Project-local Git ignore rules are applied without executing Git. Global Git
configuration is intentionally not read. A scan limit, path race, or unreadable
source is an explicit failure; it is never a silently incomplete fresh snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pathspec import GitIgnoreSpec

from ..errors import AgentError
from ..models import Project
from ..paths import ProjectPathResolver, validate_project_root
from .types import (
    EXCLUDED_DIR_NAMES,
    MAX_FILE_SIZE_BYTES,
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
    SnapshotEntry,
    WorkspaceSnapshot,
    _new_generation,
)

_MAX_SCAN_SECONDS = 20.0
_MAX_DIRECTORY_ENTRIES = 250_000
_CONTROL_NAMES = frozenset({".gitignore", ".coditoignore"})
_SECRET_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)
_SECRET_EXTENSIONS = frozenset({".pem", ".key", ".pfx", ".p12", ".jks", ".der", ".cer", ".crt"})
_BINARY_EXTENSIONS = frozenset(
    {
        ".7z",
        ".a",
        ".avi",
        ".bin",
        ".bmp",
        ".bz2",
        ".class",
        ".db",
        ".dll",
        ".dylib",
        ".exe",
        ".flac",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".lockb",
        ".mov",
        ".mp3",
        ".mp4",
        ".o",
        ".obj",
        ".otf",
        ".pdf",
        ".png",
        ".pyc",
        ".pyo",
        ".rar",
        ".so",
        ".sqlite",
        ".sqlite3",
        ".tar",
        ".tiff",
        ".ttf",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".xz",
        ".zip",
        ".zst",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
    }
)
_EXTRA_EXCLUDED = frozenset(
    {".run", ".test-work", ".codebase-memory", "artifacts", "installer-output"}
)
_IgnoreRules = tuple[tuple[str, GitIgnoreSpec], ...]


def _is_excluded_dir(name: str) -> bool:
    lower = name.lower()
    return lower in EXCLUDED_DIR_NAMES or lower in _EXTRA_EXCLUDED or lower.endswith(".egg-info")


def _should_include_file(name: str) -> bool:
    lower = name.lower()
    suffix = Path(lower).suffix
    return not (
        lower in _SECRET_NAMES
        or lower.startswith(".env.")
        or suffix in _SECRET_EXTENSIONS
        or suffix in _BINARY_EXTENSIONS
    )


def _ignored(relative: str, directory: bool, rules: _IgnoreRules) -> bool:
    excluded = False
    for base, spec in rules:
        prefix = base + "/" if base else ""
        if not relative.startswith(prefix):
            continue
        candidate = relative[len(prefix) :] + ("/" if directory else "")
        if any(
            pattern.include is not None and pattern.match_file(candidate) is not None
            for pattern in spec.patterns
        ):
            excluded = spec.match_file(candidate)
    return excluded


def _safe_project(root: Path, expected_identity: str | None) -> Project:
    validated_root, fingerprint = validate_project_root(root)
    if expected_identity is not None and fingerprint != expected_identity:
        raise AgentError("project_identity_changed", "Analysis project identity changed")
    return Project(
        project_id="code_snapshot_project",
        title="Code snapshot",
        root=validated_root,
        root_fingerprint=fingerprint,
    )


def scan_workspace(
    root: Path,
    *,
    expected_identity: str | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, SnapshotEntry]:
    """Hash every eligible source; never follow links or return partial success."""
    project = _safe_project(root, expected_identity)
    resolver = ProjectPathResolver()
    result: dict[str, SnapshotEntry] = {}
    started = time.monotonic()
    total_bytes = 0
    entry_count = 0

    def check_budget() -> None:
        if check_cancelled is not None:
            check_cancelled()
        if time.monotonic() - started > _MAX_SCAN_SECONDS:
            raise AgentError("deadline_exceeded", "Code snapshot exceeded its scan time budget")
        if entry_count > _MAX_DIRECTORY_ENTRIES or len(result) > MAX_SNAPSHOT_FILES:
            raise AgentError("result_limit", "Code snapshot exceeded its file/directory limit")
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise AgentError("result_limit", "Code snapshot exceeded its total source byte limit")

    def read_source(relative: str) -> bytes:
        nonlocal total_bytes
        check_budget()
        target = resolver.resolve(project, relative, must_exist=True, directory=False)
        if target.absolute.stat().st_size > MAX_FILE_SIZE_BYTES:
            raise AgentError(
                "file_too_large",
                "A source file exceeds the code-analysis size limit",
                {"path": relative},
            )
        with resolver.open_read(project, relative) as stream:
            data = stream.read(MAX_FILE_SIZE_BYTES + 1)
        if len(data) > MAX_FILE_SIZE_BYTES:
            raise AgentError(
                "file_too_large",
                "A source file exceeds the code-analysis size limit",
                {"path": relative},
            )
        total_bytes += len(data)
        check_budget()
        return data

    def record(relative: str, data: bytes, mtime_ns: int) -> None:
        result[relative] = SnapshotEntry(
            mtime_ns=mtime_ns, size=len(data), content_hash=hashlib.sha256(data).hexdigest()
        )
        check_budget()

    stack: list[tuple[str, _IgnoreRules]] = [("", ())]
    while stack:
        relative_dir, inherited = stack.pop()
        check_budget()
        directory = resolver.resolve(
            project, relative_dir, must_exist=True, directory=True, allow_root=True
        ).absolute
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    entry_count += 1
                    check_budget()
                    entries.append(entry)
            entries.sort(key=lambda entry: entry.name)
            rules = inherited
            for entry in entries:
                if entry.name not in _CONTROL_NAMES:
                    continue
                relative = f"{relative_dir}/{entry.name}".lstrip("/")
                data = read_source(relative)
                try:
                    control = data.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise AgentError("unsupported_encoding", "Ignore rules must be UTF-8") from exc
                record(relative, data, entry.stat(follow_symlinks=False).st_mtime_ns)
                rules += ((relative_dir, GitIgnoreSpec.from_lines(control.splitlines())),)
            for entry in entries:
                check_budget()
                if entry.name in _CONTROL_NAMES or _is_excluded_dir(entry.name):
                    continue
                relative = f"{relative_dir}/{entry.name}".lstrip("/")
                metadata = entry.stat(follow_symlinks=False)
                directory_entry = stat.S_ISDIR(metadata.st_mode)
                if _ignored(relative, directory_entry, rules):
                    continue
                if entry.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise AgentError(
                        "unsafe_path",
                        "Code analysis does not follow reparse/symlink paths",
                        {"path": relative},
                    )
                if directory_entry:
                    stack.append((relative, rules))
                    continue
                if not stat.S_ISREG(metadata.st_mode) or not _should_include_file(entry.name):
                    continue
                data = read_source(relative)
                if b"\0" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                    continue  # Explicit binary-source exclusion, not a partial text analysis.
                record(relative, data, metadata.st_mtime_ns)
        except FileNotFoundError as exc:
            raise AgentError("path_race", "Workspace changed during code snapshot") from exc
        except PermissionError as exc:
            raise AgentError(
                "read_failed", "A source path is not readable for code analysis"
            ) from exc
    resolver.verify_project(project)
    return result


def check_freshness(snapshot: WorkspaceSnapshot) -> tuple[bool, set[str], set[str], set[str]]:
    current = scan_workspace(
        snapshot.project_root, expected_identity=getattr(snapshot, "root_identity", None)
    )
    old = snapshot.file_states
    added, deleted = set(current) - set(old), set(old) - set(current)
    modified = {path for path in set(current) & set(old) if not current[path].matches(old[path])}
    return not (added or modified or deleted), added, modified, deleted


def take_snapshot(
    root: Path,
    previous: WorkspaceSnapshot | None = None,
    *,
    expected_identity: str | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> WorkspaceSnapshot:
    identity = expected_identity or (getattr(previous, "root_identity", None) if previous else None)
    return WorkspaceSnapshot(
        generation=_new_generation(),
        project_root=root,
        file_states=scan_workspace(
            root, expected_identity=identity, check_cancelled=check_cancelled
        ),
        root_identity=identity or _safe_project(root, None).root_fingerprint,
    )


async def capture_snapshot(
    root: Path, previous: WorkspaceSnapshot | None = None, *, expected_identity: str | None = None
) -> WorkspaceSnapshot:
    """Drain the file reader on cancellation; no worker keeps reading after revoke."""
    cancelled = threading.Event()

    def check() -> None:
        if cancelled.is_set():
            raise AgentError("approval_expired", "Code snapshot was cancelled")

    task = asyncio.create_task(
        asyncio.to_thread(
            take_snapshot,
            root,
            previous,
            expected_identity=expected_identity,
            check_cancelled=check,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        await asyncio.gather(task, return_exceptions=True)
        raise
