from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import pytest

from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.read_tools import ProjectReadService


def test_read_directory_file_and_search(tmp_path: Path, project_root: Path) -> None:
    (project_root / "src").mkdir()
    (project_root / "src" / "hello.py").write_bytes(b"first\r\nhello Codito\r\nthird\r\n")
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = ProjectReadService(database)

    listed = service.execute(
        {
            "operation": "list_directory",
            "project_id": project.project_id,
            "path": "src",
        }
    )
    assert listed.structured["entries"][0]["path"] == "src/hello.py"

    read = service.execute(
        {
            "operation": "read_file",
            "project_id": project.project_id,
            "path": "src/hello.py",
            "start_line": 2,
            "end_line": 2,
        }
    )
    assert "2 | hello Codito" in read.structured["numbered_text"]
    assert read.structured["newline"] == "crlf"
    assert len(read.structured["sha256"]) == 64

    searched = service.execute(
        {
            "operation": "search_text",
            "project_id": project.project_id,
            "query": "codito",
            "glob": "**/*.py",
        }
    )
    assert searched.structured["matches"][0]["line"] == 2


def test_search_text_accepts_a_single_file_path(tmp_path: Path, project_root: Path) -> None:
    target = project_root / "src" / "hello.py"
    target.parent.mkdir()
    target.write_text("first\nneedle\nthird\n", encoding="utf-8")
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)

    result = ProjectReadService(database).search_text(
        project.project_id,
        query="needle",
        path="src/hello.py",
        globs=["**/*.py"],
    )

    assert result.structured["matches"] == [
        {"path": "src/hello.py", "line": 2, "column": 1, "preview": "needle"}
    ]


def test_missing_file_is_reported_as_path_not_found(tmp_path: Path, project_root: Path) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)

    with pytest.raises(AgentError) as error:
        ProjectReadService(database).read_file(project.project_id, "missing.txt")

    assert error.value.code == "path_not_found"


def test_read_continuation_is_bounded(tmp_path: Path, project_root: Path) -> None:
    (project_root / "long.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = ProjectReadService(database)
    first = service.read_file(project.project_id, "long.txt", max_bytes=5)
    assert first.structured["truncated"]
    second = service.read_file(
        project.project_id,
        "long.txt",
        max_bytes=100,
        cursor=first.structured["continuation"],
    )
    assert "two" in second.structured["text"]


def test_recursive_directory_glob_matches_project_relative_path(
    tmp_path: Path, project_root: Path
) -> None:
    (project_root / "src" / "nested").mkdir(parents=True)
    (project_root / "src" / "nested" / "module.py").write_text("pass\n")
    (project_root / "src" / "nested" / "ignore.txt").write_text("ignore\n")
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    result = ProjectReadService(database).list_directory(
        project.project_id,
        ".",
        glob="src/**/*.py",
        recursive=True,
    )
    assert [entry["path"] for entry in result.structured["entries"]] == ["src/nested/module.py"]


def test_regex_search_has_execution_timeout(tmp_path: Path, project_root: Path) -> None:
    (project_root / "input.txt").write_text("a" * 500_000 + "!", encoding="utf-8")
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    with pytest.raises(AgentError) as error:
        ProjectReadService(database).search_text(
            project.project_id,
            query=r"^(a+)+$",
            regex=True,
            limit=1,
        )
    assert error.value.code == "search_timeout"


def test_read_hash_and_size_describe_the_handle_validated_bytes(
    tmp_path: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = b"old\n"
    current = b"new content\r\n"
    target = project_root / "file.txt"
    target.write_bytes(old)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = ProjectReadService(database)
    original_open = service.resolver.open_read

    @contextmanager
    def replace_before_open(project_value: object, relative: str) -> Iterator[BinaryIO]:
        target.write_bytes(current)
        with original_open(project_value, relative) as stream:  # type: ignore[arg-type]
            yield stream

    monkeypatch.setattr(service.resolver, "open_read", replace_before_open)
    result = service.read_file(project.project_id, "file.txt")
    assert result.structured["size"] == len(current)
    assert result.structured["sha256"] == hashlib.sha256(current).hexdigest()
    assert result.structured["newline"] == "crlf"


def test_utf16_newlines_are_classified_after_decoding(tmp_path: Path, project_root: Path) -> None:
    raw = "first\r\nsecond\r\n".encode("utf-16")
    (project_root / "utf16.txt").write_bytes(raw)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    result = ProjectReadService(database).read_file(project.project_id, "utf16.txt")
    assert result.structured["newline"] == "crlf"
    assert result.structured["size"] == len(raw)
    assert result.structured["sha256"] == hashlib.sha256(raw).hexdigest()
