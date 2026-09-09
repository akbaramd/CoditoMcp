from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import regex as regex_module  # type: ignore[import-untyped]

from .db import AgentDatabase
from .errors import AgentError
from .models import Project
from .paths import ProjectPathResolver, _is_reparse, validate_relative_path

MAX_READ_BYTES = 2_097_152
MAX_SEARCH_FILE_BYTES = 2_097_152
MAX_SEARCH_SCANNED_BYTES = 16_777_216
MAX_DIRECTORY_RESULTS = 1000
MAX_SEARCH_RESULTS = 500
MAX_DIRECTORY_SCANNED = 10_000
MAX_SEARCH_FILES = 10_000


@dataclass(frozen=True, slots=True)
class ToolResponse:
    structured: dict[str, Any]
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"structured": self.structured, "text": self.text}


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        offset = int(decoded)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AgentError("invalid_cursor", "Continuation cursor is invalid") from exc
    if offset < 0:
        raise AgentError("invalid_cursor", "Continuation cursor is invalid")
    return offset


def _encode_read_cursor(line_index: int, character_offset: int) -> str:
    raw = f"{line_index}:{character_offset}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_read_cursor(value: str) -> tuple[int, int]:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        line, character = (int(part) for part in decoded.split(":", 1))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AgentError("invalid_cursor", "Continuation cursor is invalid") from exc
    if line < 0 or character < 0:
        raise AgentError("invalid_cursor", "Continuation cursor is invalid")
    return line, character


def _detect_text(raw: bytes) -> tuple[str, str, bytes]:
    if b"\x00" in raw[:8192] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise AgentError("binary_file", "Binary files cannot be returned as text")
    candidates: list[tuple[str, bytes]] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.append(("utf-8-sig", raw))
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append(("utf-16", raw))
    else:
        candidates.append(("utf-8", raw))
    for encoding, value in candidates:
        try:
            decoded = value.decode(encoding)
            if encoding == "utf-16":
                encoding = "utf-16-le" if value.startswith(b"\xff\xfe") else "utf-16-be"
            return decoded, encoding, value
        except UnicodeDecodeError:
            continue
    raise AgentError("unsupported_encoding", "File is not valid UTF-8 or BOM-marked UTF-16")


def _newline_style(text: str) -> str:
    """Classify newlines after decoding so UTF-16 NUL bytes cannot hide them."""

    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    kinds = sum(value > 0 for value in (crlf, lf, cr))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if lf:
        return "lf"
    if cr:
        return "cr"
    return "none"


def _glob_matches(path: str, pattern: str) -> bool:
    """Match project-relative globs with `**/` also representing zero directories."""

    candidates = {pattern}
    reduced = pattern
    while "**/" in reduced:
        reduced = reduced.replace("**/", "", 1)
        candidates.add(reduced)
    return any(fnmatch.fnmatchcase(path, candidate) for candidate in candidates)


def _payload_codec(encoding: str) -> str:
    return "utf-8" if encoding == "utf-8-sig" else encoding


class ProjectReadService:
    def __init__(
        self, database: AgentDatabase, resolver: ProjectPathResolver | None = None
    ) -> None:
        self.database = database
        self.resolver = resolver or ProjectPathResolver()

    def execute(
        self, request: dict[str, Any], *, target_project: Project | None = None
    ) -> ToolResponse:
        operation = request.get("operation")
        if operation == "list_projects":
            return self.list_projects()
        project_id = request.get("project_id")
        if not isinstance(project_id, str):
            raise AgentError("invalid_request", "project_id is required")
        project = self.database.get_project(project_id)
        if not project.enabled:
            raise AgentError("project_disabled", "The requested project is disabled")
        if operation == "list_directory":
            return self.list_directory(
                project_id,
                str(request.get("path", ".")),
                limit=int(request.get("limit", 100)),
                cursor=request.get("cursor"),
                glob=request.get("glob"),
                recursive=bool(request.get("recursive", False)),
                target_project=target_project,
            )
        if operation == "read_file":
            return self.read_file(
                project_id,
                str(request.get("path", "")),
                start_line=request.get("start_line"),
                end_line=request.get("end_line"),
                max_bytes=int(request.get("max_bytes", 262_144)),
                max_lines=request.get("max_lines"),
                cursor=request.get("continuation", request.get("cursor")),
                target_project=target_project,
            )
        if operation == "search_text":
            return self.search_text(
                project_id,
                query=request.get("query"),
                path=str(request.get("path", ".")),
                globs=[request["glob"]] if "glob" in request else request.get("globs"),
                case_sensitive=bool(request.get("case_sensitive", False)),
                regex=bool(request.get("regex", False)),
                max_bytes_per_match=int(request.get("max_bytes_per_match", 2048)),
                limit=int(request.get("max_results", request.get("limit", 50))),
                cursor=request.get("continuation", request.get("cursor")),
                target_project=target_project,
            )
        raise AgentError("invalid_request", "Unsupported project_read operation")

    def list_projects(self) -> ToolResponse:
        projects = [project.public_dict() for project in self.database.list_projects()]
        return ToolResponse(
            {"operation": "list_projects", "projects": projects, "count": len(projects)},
            f"Found {len(projects)} registered project(s).",
        )

    def list_directory(
        self,
        project_id: str,
        relative: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
        glob: str | None = None,
        recursive: bool = False,
        target_project: Project | None = None,
    ) -> ToolResponse:
        if not 1 <= limit <= MAX_DIRECTORY_RESULTS:
            raise AgentError("invalid_request", "Directory result limit is out of range")
        if glob is not None:
            self._validate_glob(glob)
        project = target_project or self.database.get_project(project_id)
        directory = self.resolver.resolve(
            project, relative, directory=True, allow_root=True
        ).absolute
        entries: list[dict[str, Any]] = []
        children: list[Path] = []
        if recursive:
            for current, directories, files in os.walk(directory, followlinks=False):
                current_path = Path(current)
                directories[:] = [
                    name for name in directories if not _is_reparse(current_path / name)
                ]
                children.extend(current_path / name for name in directories)
                children.extend(current_path / name for name in files)
        else:
            children = list(directory.iterdir())
        if len(children) > MAX_DIRECTORY_SCANNED:
            raise AgentError(
                "result_limit", "Directory contains too many entries to enumerate safely"
            )
        for child in children:
            child_relative = child.relative_to(project.root).as_posix()
            relative_to_base = child.relative_to(directory).as_posix()
            if glob is not None and not (
                _glob_matches(child_relative, glob) or _glob_matches(relative_to_base, glob)
            ):
                continue
            is_link = _is_reparse(child)
            if is_link:
                continue
            info = child.lstat()
            entries.append(
                {
                    "name": child.name,
                    "path": child_relative,
                    "kind": "directory" if child.is_dir() else "file",
                    "size": info.st_size if child.is_file() and not is_link else None,
                }
            )
        entries.sort(key=lambda item: (item["kind"] != "directory", str(item["name"]).casefold()))
        offset = _decode_cursor(cursor)
        page = entries[offset : offset + limit]
        next_offset = offset + len(page)
        continuation = _encode_cursor(next_offset) if next_offset < len(entries) else None
        structured = {
            "operation": "list_directory",
            "project_id": project_id,
            "path": validate_relative_path(relative, allow_root=True),
            "entries": page,
            "truncated": continuation is not None,
            "continuation": continuation,
        }
        return ToolResponse(
            structured, f"Listed {len(page)} entr{'y' if len(page) == 1 else 'ies'}."
        )

    def read_file(
        self,
        project_id: str,
        relative: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int = 262_144,
        max_lines: int | None = None,
        cursor: str | None = None,
        target_project: Project | None = None,
    ) -> ToolResponse:
        if not 1 <= max_bytes <= MAX_READ_BYTES:
            raise AgentError("invalid_request", "max_bytes is out of range")
        if max_lines is not None and not 1 <= max_lines <= 2000:
            raise AgentError("invalid_request", "max_lines is out of range")
        if cursor and (start_line is not None or end_line is not None):
            raise AgentError("invalid_request", "cursor cannot be combined with a line range")
        if start_line is not None and (not isinstance(start_line, int) or start_line < 1):
            raise AgentError("invalid_request", "start_line must be a positive integer")
        if end_line is not None and (not isinstance(end_line, int) or end_line < 1):
            raise AgentError("invalid_request", "end_line must be a positive integer")
        if start_line and end_line and end_line < start_line:
            raise AgentError("invalid_request", "end_line must not precede start_line")

        project = target_project or self.database.get_project(project_id)
        with self.resolver.open_read(project, relative) as stream:
            raw = stream.read(MAX_READ_BYTES * 8 + 1)
        size = len(raw)
        if size > MAX_READ_BYTES * 8:
            raise AgentError("file_too_large", "Text reads are limited to files of 16 MiB")
        digest = hashlib.sha256(raw).hexdigest()
        text_all, encoding, _ = _detect_text(raw)
        lines = text_all.splitlines(keepends=True)
        if cursor:
            line_index, character_offset = _decode_read_cursor(cursor)
        else:
            line_index, character_offset = (start_line or 1) - 1, 0
        if line_index > len(lines) or (line_index == len(lines) and character_offset):
            raise AgentError("invalid_cursor", "Continuation cursor exceeds the file")
        first_number = line_index + 1
        stop_index = min(end_line, len(lines)) if end_line is not None else len(lines)
        if max_lines is not None:
            stop_index = min(stop_index, line_index + max_lines)
        selected_parts: list[str] = []
        consumed = 0
        next_line, next_character = line_index, character_offset
        for index in range(line_index, stop_index):
            part = lines[index][character_offset:] if index == line_index else lines[index]
            encoded = part.encode(_payload_codec(encoding))
            remaining = max_bytes - consumed
            if len(encoded) <= remaining:
                selected_parts.append(part)
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
                selected_parts.append(part[:low])
                next_line, next_character = (
                    index,
                    (character_offset if index == line_index else 0) + low,
                )
            break
        text = "".join(selected_parts)
        truncated = next_line < stop_index or next_character > 0
        next_cursor = _encode_read_cursor(next_line, next_character) if truncated else None

        numbered = "".join(
            f"{number:>6} | {line}"
            for number, line in enumerate(text.splitlines(keepends=True), first_number)
        )
        structured = {
            "operation": "read_file",
            "project_id": project_id,
            "path": validate_relative_path(relative),
            "text": text,
            "numbered_text": numbered,
            "encoding": encoding,
            "newline": _newline_style(text_all),
            "size": size,
            "sha256": digest,
            "first_line": first_number,
            "last_line": first_number + max(0, len(text.splitlines()) - 1) if text else 0,
            "truncated": truncated,
            "continuation": next_cursor,
        }
        return ToolResponse(
            structured,
            f"Read {len(text.encode(_payload_codec(encoding)))} byte(s) from the file.",
        )

    def search_text(
        self,
        project_id: str,
        *,
        query: Any,
        path: str = ".",
        globs: Any = None,
        case_sensitive: bool = False,
        regex: bool = False,
        max_bytes_per_match: int = 2048,
        limit: int = 50,
        cursor: str | None = None,
        target_project: Project | None = None,
    ) -> ToolResponse:
        if not isinstance(query, str) or not query or len(query) > 4096:
            raise AgentError("invalid_request", "query must contain 1 to 4096 characters")
        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise AgentError("invalid_request", "Search result limit is out of range")
        if not 128 <= max_bytes_per_match <= 16_384:
            raise AgentError("invalid_request", "max_bytes_per_match is out of range")
        patterns = ["*"] if globs is None else globs
        if not isinstance(patterns, list) or not 1 <= len(patterns) <= 20:
            raise AgentError("invalid_request", "globs must be a list of at most 20 patterns")
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise AgentError("invalid_request", "Every glob must be text")
            self._validate_glob(pattern)

        project = target_project or self.database.get_project(project_id)
        result_offset = _decode_cursor(cursor)
        target = self.resolver.resolve(project, path, directory=None, allow_root=True).absolute
        candidates: list[Path] = []
        if target.is_file():
            relative = target.relative_to(project.root).as_posix()
            if any(_glob_matches(relative, pattern) for pattern in patterns):
                candidates.append(target)
        else:
            for current, directories, files in os.walk(target, followlinks=False):
                current_path = Path(current)
                directories[:] = [
                    name for name in directories if not _is_reparse(current_path / name)
                ]
                for name in files:
                    candidate = current_path / name
                    if _is_reparse(candidate):
                        continue
                    relative = candidate.relative_to(project.root).as_posix()
                    if any(_glob_matches(relative, pattern) for pattern in patterns):
                        candidates.append(candidate)
                        if len(candidates) > MAX_SEARCH_FILES:
                            raise AgentError(
                                "result_limit", "Project has too many search candidates"
                            )
        candidates.sort(key=lambda value: value.relative_to(project.root).as_posix().casefold())

        needle = query if case_sensitive else query.casefold()
        expression = None
        if regex:
            try:
                expression = regex_module.compile(
                    query, 0 if case_sensitive else regex_module.IGNORECASE
                )
            except regex_module.error as exc:
                raise AgentError("invalid_request", "Search regular expression is invalid") from exc
        all_matches: list[dict[str, Any]] = []
        scanned = 0
        exhausted = False
        for candidate in candidates:
            size = candidate.stat().st_size
            if size > MAX_SEARCH_FILE_BYTES:
                continue
            scanned += size
            if scanned > MAX_SEARCH_SCANNED_BYTES:
                exhausted = True
                break
            relative = candidate.relative_to(project.root).as_posix()
            try:
                with self.resolver.open_read(project, relative) as stream:
                    raw = stream.read(MAX_SEARCH_FILE_BYTES + 1)
                text, _, _ = _detect_text(raw)
            except AgentError as exc:
                if exc.code in {"binary_file", "unsupported_encoding"}:
                    continue
                raise
            for line_number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                try:
                    match = (
                        expression.search(line, timeout=0.05) if expression is not None else None
                    )
                except TimeoutError as exc:
                    raise AgentError(
                        "search_timeout", "Regular expression exceeded its execution budget"
                    ) from exc
                column = match.start() if match is not None else haystack.find(needle)
                if column >= 0:
                    all_matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "column": column + 1,
                            "preview": line.encode("utf-8")[:max_bytes_per_match].decode(
                                "utf-8", errors="ignore"
                            ),
                        }
                    )
                    if len(all_matches) >= result_offset + limit + 1:
                        exhausted = True
                        break
            if exhausted and len(all_matches) >= result_offset + limit + 1:
                break

        offset = result_offset
        page = all_matches[offset : offset + limit]
        next_offset = offset + len(page)
        more = next_offset < len(all_matches) or exhausted
        structured = {
            "operation": "search_text",
            "project_id": project_id,
            "query": query,
            "matches": page,
            "truncated": more,
            "continuation": _encode_cursor(next_offset) if more else None,
            "scanned_bytes": scanned,
        }
        return ToolResponse(structured, f"Found {len(page)} matching line(s).")

    @staticmethod
    def _validate_glob(pattern: str) -> None:
        if not pattern or len(pattern) > 200 or "\\" in pattern or "\x00" in pattern:
            raise AgentError("invalid_glob", "Glob is empty or malformed")
        if pattern.startswith(("/", "//")) or ":" in pattern:
            raise AgentError("invalid_glob", "Glob must be project-relative")
        if any(part == ".." for part in pattern.split("/")):
            raise AgentError("invalid_glob", "Parent traversal is forbidden in globs")
