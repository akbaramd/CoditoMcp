"""Bounded reads below a locally authorized, handle-pinned Windows directory.

Path names are only display/selection data. Every directory is pinned before
enumeration, and every file is read through WindowsReadScope's validated handle.
The caller retains the scope until this synchronous worker has fully drained.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import regex as regex_module  # type: ignore[import-untyped]

from .device_paths import WindowsReadScope
from .errors import AgentError
from .paths import validate_relative_path
from .read_tools import (
    MAX_DIRECTORY_RESULTS,
    MAX_DIRECTORY_SCANNED,
    MAX_READ_BYTES,
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_RESULTS,
    MAX_SEARCH_SCANNED_BYTES,
    ProjectReadService,
    ToolResponse,
    _decode_cursor,
    _decode_read_cursor,
    _detect_text,
    _encode_cursor,
    _encode_read_cursor,
    _glob_matches,
    _newline_style,
    _payload_codec,
)

_BLOCKED_ATTRIBUTES = 0x400 | 0x1000 | 0x40000 | 0x400000
_MAX_DEPTH = 128


@dataclass(frozen=True, slots=True)
class _Entry:
    path: str
    kind: str
    size: int | None


def _walk(
    scope: WindowsReadScope,
    relative: str,
    *,
    recursive: bool,
    check: Callable[[], None],
) -> Iterator[_Entry]:
    base = validate_relative_path(relative, allow_root=True)
    pending = [(base, 0)]
    scanned = 0
    while pending:
        check()
        directory, depth = pending.pop()
        if depth > _MAX_DEPTH:
            raise AgentError("result_limit", "Directory depth exceeds the safe scan limit")
        # Pins this path and all ancestors against rename and reparse conversion.
        pinned_directory = scope.directory(directory)
        with os.scandir(pinned_directory) as entries:
            for entry in entries:
                check()
                scanned += 1
                if scanned > MAX_DIRECTORY_SCANNED:
                    raise AgentError("result_limit", "Directory scan exceeded its entry budget")
                try:
                    child = validate_relative_path(
                        str(PurePosixPath(directory) / entry.name), allow_root=False
                    )
                    info = entry.stat(follow_symlinks=False)
                except (AgentError, OSError):
                    continue
                if getattr(info, "st_file_attributes", 0) & _BLOCKED_ATTRIBUTES:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    # Do not enumerate this child until scope.directory pins it.
                    yield _Entry(child, "directory", None)
                    if recursive:
                        pending.append((child, depth + 1))
                elif stat.S_ISREG(info.st_mode):
                    try:
                        size = scope.file_size(child)
                    except AgentError as exc:
                        if exc.code in {"unsafe_path", "read_failed"}:
                            continue
                        raise
                    yield _Entry(child, "file", size)


def _pattern(request: dict[str, Any], default: str) -> str:
    pattern = request.get("glob", default)
    if not isinstance(pattern, str):
        raise AgentError("invalid_glob", "Glob must be text")
    ProjectReadService._validate_glob(pattern)
    return pattern


def _matches(path: str, base: str, pattern: str) -> bool:
    below_base = str(PurePosixPath(path).relative_to(PurePosixPath(base)))
    return _glob_matches(path, pattern) or _glob_matches(below_base, pattern)


def _list_directory(
    scope: WindowsReadScope, request: dict[str, Any], check: Callable[[], None]
) -> ToolResponse:
    path = validate_relative_path(str(request.get("path", "")), allow_root=True)
    limit = int(request.get("limit", 200))
    if not 1 <= limit <= MAX_DIRECTORY_RESULTS:
        raise AgentError("invalid_request", "Directory result limit is out of range")
    pattern = _pattern(request, "*")
    offset = _decode_cursor(request.get("cursor"))
    if offset > MAX_DIRECTORY_SCANNED:
        raise AgentError("invalid_cursor", "Directory cursor exceeds the scan limit")
    entries = [
        entry
        for entry in _walk(
            scope, path, recursive=bool(request.get("recursive", False)), check=check
        )
        if _matches(entry.path, path, pattern)
    ]
    entries.sort(key=lambda entry: (entry.kind != "directory", entry.path.casefold(), entry.path))
    if offset > len(entries):
        raise AgentError("invalid_cursor", "Directory cursor exceeds current entries")
    page = entries[offset : offset + limit]
    next_offset = offset + len(page)
    cursor = _encode_cursor(next_offset) if next_offset < len(entries) else None
    return ToolResponse(
        {
            "operation": "list_directory",
            "project_id": request["project_id"],
            "path": path,
            "entries": [
                {"path": entry.path, "kind": entry.kind, "size": entry.size} for entry in page
            ],
            "truncated": cursor is not None,
            "continuation": cursor,
        },
        f"Listed {len(page)} entries from the approved directory.",
    )


def _read_file(
    scope: WindowsReadScope, request: dict[str, Any], check: Callable[[], None]
) -> ToolResponse:
    path = validate_relative_path(str(request.get("path", "")))
    maximum = int(request.get("max_bytes", 262_144))
    if not 1 <= maximum <= MAX_READ_BYTES:
        raise AgentError("invalid_request", "max_bytes is out of range")
    cursor = request.get("continuation") or request.get("cursor")
    start = request.get("start_line")
    stop = request.get("end_line")
    maximum_lines = request.get("max_lines")
    if cursor and stop is not None:
        raise AgentError("invalid_request", "Continuation cannot be combined with an end line")
    if start is not None and (not isinstance(start, int) or start < 1):
        raise AgentError("invalid_request", "start_line must be positive")
    if stop is not None and (not isinstance(stop, int) or stop < (start or 1)):
        raise AgentError("invalid_request", "end_line precedes the requested range")
    if maximum_lines is not None and (not isinstance(maximum_lines, int) or maximum_lines < 1):
        raise AgentError("invalid_request", "max_lines must be positive")
    raw = scope.read_bytes(path, check, release_file=True)
    check()
    content, encoding, _ = _detect_text(raw)
    lines = content.splitlines(keepends=True)
    line, character = _decode_read_cursor(cursor) if cursor else ((start or 1) - 1, 0)
    if line > len(lines) or (line == len(lines) and character):
        raise AgentError("invalid_cursor", "Continuation exceeds this file")
    if line < len(lines) and character > len(lines[line]):
        raise AgentError("invalid_cursor", "Continuation exceeds this line")
    requested_stop = min(stop, len(lines)) if stop is not None else len(lines)
    stop_index = min(requested_stop, line + maximum_lines) if maximum_lines else requested_stop
    selected: list[str] = []
    consumed = 0
    next_line, next_character = line, character
    for index in range(line, stop_index):
        check()
        part = lines[index][character:] if index == line else lines[index]
        remaining = maximum - consumed
        encoded = part.encode(_payload_codec(encoding))
        if len(encoded) <= remaining:
            selected.append(part)
            consumed += len(encoded)
            next_line, next_character = index + 1, 0
            continue
        low, high = 0, len(part)
        while low < high:
            middle = (low + high + 1) // 2
            if len(part[:middle].encode(_payload_codec(encoding))) <= remaining:
                low = middle
            else:
                high = middle - 1
        if low:
            selected.append(part[:low])
            next_line, next_character = index, (character if index == line else 0) + low
        elif not selected:
            raise AgentError("result_limit", "max_bytes cannot hold the next encoded character")
        break
    text = "".join(selected)
    truncated = next_line < requested_stop or next_character > 0
    return ToolResponse(
        {
            "operation": "read_file",
            "project_id": request["project_id"],
            "path": path,
            "numbered_text": "".join(
                f"{number:>6} | {part}"
                for number, part in enumerate(text.splitlines(keepends=True), line + 1)
            ),
            "text": text,
            "encoding": encoding,
            "newline": _newline_style(content),
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "first_line": line + 1,
            "last_line": line + len(text.splitlines()) if text else 0,
            "truncated": truncated,
            "continuation": _encode_read_cursor(next_line, next_character) if truncated else None,
        },
        f"Read {len(text.encode(_payload_codec(encoding)))} content bytes from an approved handle.",
    )


def _search_text(
    scope: WindowsReadScope, request: dict[str, Any], check: Callable[[], None]
) -> ToolResponse:
    path = validate_relative_path(str(request.get("path", "")), allow_root=True)
    query = request.get("query")
    if not isinstance(query, str) or not 1 <= len(query) <= 4096:
        raise AgentError("invalid_request", "Search query must contain 1 to 4096 characters")
    limit = int(request.get("max_results", 100))
    preview_limit = int(request.get("max_bytes_per_match", 2048))
    if not 1 <= limit <= MAX_SEARCH_RESULTS or not 128 <= preview_limit <= 16_384:
        raise AgentError("invalid_request", "Search result limit is out of range")
    pattern = _pattern(request, "**/*")
    cursor = request.get("continuation")
    file_index, start_line = _decode_read_cursor(cursor) if cursor else (0, 0)
    entries = [
        entry
        for entry in _walk(scope, path, recursive=True, check=check)
        if entry.kind == "file" and _matches(entry.path, path, pattern)
    ]
    entries.sort(key=lambda entry: (entry.path.casefold(), entry.path))
    if file_index > len(entries) or (file_index == len(entries) and start_line):
        raise AgentError("invalid_cursor", "Search cursor exceeds current files")
    case_sensitive = bool(request.get("case_sensitive", False))
    needle = query if case_sensitive else query.casefold()
    expression = None
    if request.get("regex"):
        try:
            expression = regex_module.compile(
                query, 0 if case_sensitive else regex_module.IGNORECASE
            )
        except regex_module.error as exc:
            raise AgentError("invalid_request", "Search regular expression is invalid") from exc
    matches: list[dict[str, Any]] = []
    scanned = 0
    continuation = None
    for index in range(file_index, len(entries)):
        check()
        entry = entries[index]
        if entry.size is not None and entry.size > MAX_SEARCH_FILE_BYTES:
            continue
        if scanned and scanned + (entry.size or 0) > MAX_SEARCH_SCANNED_BYTES:
            continuation = _encode_read_cursor(index, 0)
            break
        remaining = min(MAX_SEARCH_FILE_BYTES, MAX_SEARCH_SCANNED_BYTES - scanned)
        try:
            raw = scope.read_bytes(entry.path, check, release_file=True, max_bytes=remaining)
        except AgentError as exc:
            if exc.code == "file_too_large":
                if scanned:
                    continuation = _encode_read_cursor(index, 0)
                    break
                continue
            if exc.code == "unsafe_path":
                continue
            raise
        scanned += len(raw)
        if len(raw) > MAX_SEARCH_FILE_BYTES:
            continue
        try:
            content, _, _ = _detect_text(raw)
        except AgentError as exc:
            if exc.code in {"binary_file", "unsupported_encoding"}:
                continue
            raise
        lines = content.splitlines()
        first_line = start_line if index == file_index else 0
        if first_line > len(lines):
            raise AgentError("invalid_cursor", "Search cursor exceeds the current file")
        for number in range(first_line, len(lines)):
            check()
            text = lines[number]
            try:
                match = expression.search(text, timeout=0.05) if expression is not None else None
            except TimeoutError as exc:
                raise AgentError(
                    "search_timeout", "Regular expression exceeded its time budget"
                ) from exc
            column = (
                (match.start() if match is not None else -1)
                if expression is not None
                else (text if case_sensitive else text.casefold()).find(needle)
            )
            if column >= 0:
                matches.append(
                    {
                        "path": entry.path,
                        "line": number + 1,
                        "column": column + 1,
                        "preview": text.encode("utf-8")[:preview_limit].decode(
                            "utf-8", errors="ignore"
                        ),
                    }
                )
                if len(matches) >= limit:
                    if number + 1 < len(lines):
                        continuation = _encode_read_cursor(index, number + 1)
                    elif index + 1 < len(entries):
                        continuation = _encode_read_cursor(index + 1, 0)
                    break
        if len(matches) >= limit:
            break
    return ToolResponse(
        {
            "operation": "search_text",
            "project_id": request["project_id"],
            "query": query,
            "matches": matches,
            "truncated": continuation is not None,
            "continuation": continuation,
            "scanned_bytes": scanned,
        },
        f"Found {len(matches)} matching lines through approved handles.",
    )


def read_scoped_files(
    scope: WindowsReadScope, request: dict[str, Any], check: Callable[[], None]
) -> ToolResponse:
    """Read only after the caller's approval gate; check cancellation throughout."""
    check()
    operations = {
        "list_directory": _list_directory,
        "read_file": _read_file,
        "search_text": _search_text,
    }
    operation = operations.get(str(request.get("operation")))
    if operation is None:
        raise AgentError("invalid_request", "Unsupported scoped file operation")
    try:
        result = operation(scope, request, check)
    except OSError as exc:
        raise AgentError(
            "read_failed", "Windows denied access or the approved path changed"
        ) from exc
    check()
    return result
