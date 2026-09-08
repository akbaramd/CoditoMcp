"""Only synthetic content and workspace-local temporary files are inspected."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from codito_protocol.read import ProjectReadResult
from pydantic import TypeAdapter

from codito_agent.device_paths import WindowsReadScope
from codito_agent.errors import AgentError
from codito_agent.scoped_read_tools import read_scoped_files


class FakeScope:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.data = {}

    def directory(self, relative):
        self.calls.append(("pin", relative))
        return self.root / relative

    def file_size(self, relative):
        return (self.root / relative).stat().st_size

    def read_bytes(self, relative, check, *, release_file=False, max_bytes=16 * 1024 * 1024):
        check()
        self.calls.append(("read", relative, release_file))
        raw = self.data[relative]
        if len(raw) > max_bytes:
            raise AgentError("file_too_large", "Synthetic file exceeded handle read cap")
        return raw


def request(operation, **values):
    return {"operation": operation, "project_id": "project_abcdefghijkl", **values}


def normalized(response):
    value = dict(response.structured)
    value["path"] = "" if value.get("path") == "." else value.get("path")
    if value["operation"] == "search_text":
        value.pop("path", None)
        value.pop("query", None)
        value.pop("scanned_bytes", None)
    if value["operation"] == "list_directory":
        value.pop("truncated", None)
    value.pop("text", None)
    cursor = value.get("continuation")
    value["continuation"] = {"cursor": cursor} if cursor else None
    return TypeAdapter(ProjectReadResult).validate_python(value)


@pytest.mark.parametrize(
    "encoding,bom", [("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe")]
)
def test_file_contents_only_come_from_validated_handle_and_keep_encoding_hash(
    tmp_path, encoding, bom
):
    scope = FakeScope(tmp_path)
    raw = bom + "line one\r\nخط دوم\r\n".encode(encoding)
    scope.data["missing-on-disk.txt"] = raw
    response = read_scoped_files(
        scope, request("read_file", path="missing-on-disk.txt"), lambda: None
    )
    value = normalized(response)
    assert scope.calls == [("read", "missing-on-disk.txt", True)]
    assert value.sha256 == hashlib.sha256(raw).hexdigest()
    assert value.newline == "crlf"
    assert value.first_line == 1 and value.last_line == 2
    assert "خط دوم" in value.numbered_text
    assert value.encoding == ("utf-8-sig" if bom == b"\xef\xbb\xbf" else encoding)


def test_read_max_lines_applies_to_each_continuation_page(tmp_path):
    scope = FakeScope(tmp_path)
    scope.data["lines.txt"] = b"one\ntwo\nthree\nfour\nfive\n"
    payload = request("read_file", path="lines.txt", max_lines=2)
    pages = []
    for _ in range(3):
        response = read_scoped_files(scope, payload, lambda: None)
        pages.append(normalized(response))
        payload["continuation"] = response.structured["continuation"]
    assert [(page.first_line, page.last_line) for page in pages] == [(1, 2), (3, 4), (5, 5)]
    assert pages[0].truncated and pages[1].truncated and not pages[2].truncated


def test_read_character_pagination_never_repeats_empty_cursor(tmp_path):
    scope = FakeScope(tmp_path)
    scope.data["utf.txt"] = "خط\n".encode()
    payload = request("read_file", path="utf.txt", max_bytes=2)
    parts = []
    cursors = set()
    for _ in range(3):
        response = read_scoped_files(scope, payload, lambda: None)
        parts.append(response.structured["text"])
        cursor = response.structured["continuation"]
        assert cursor not in cursors
        cursors.add(cursor)
        payload["continuation"] = cursor
    assert "".join(parts) == "خط\n"
    with pytest.raises(AgentError, match="encoded character"):
        read_scoped_files(scope, request("read_file", path="utf.txt", max_bytes=1), lambda: None)


def test_recursive_listing_pins_each_directory_before_enumerating(tmp_path, monkeypatch):
    from codito_agent import scoped_read_tools

    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.py").write_text("synthetic", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("synthetic", encoding="utf-8")
    scope = FakeScope(tmp_path)
    original_scandir = os.scandir

    def checked_scandir(path):
        relative = str(Path(path).relative_to(tmp_path)).replace("\\", "/")
        assert ("pin", relative) in scope.calls
        return original_scandir(path)

    monkeypatch.setattr(scoped_read_tools.os, "scandir", checked_scandir)
    response = read_scoped_files(
        scope, request("list_directory", recursive=True, glob="**/*.py"), lambda: None
    )
    assert [entry.path for entry in normalized(response).entries] == ["nested/a.py"]
    assert not any(call[0] == "read" for call in scope.calls)


def test_directory_scan_budget_is_enforced_during_enumeration(tmp_path, monkeypatch):
    from codito_agent import scoped_read_tools

    for index in range(4):
        (tmp_path / f"{index}.txt").write_text("test", encoding="utf-8")
    monkeypatch.setattr(scoped_read_tools, "MAX_DIRECTORY_SCANNED", 2)
    with pytest.raises(AgentError, match="entry budget"):
        read_scoped_files(FakeScope(tmp_path), request("list_directory"), lambda: None)


def test_search_respects_glob_regex_and_advances_without_duplicate_matches(tmp_path):
    scope = FakeScope(tmp_path)
    for name, raw in {
        "a.py": b"hit 1\nno match\nhit 2\n",
        "b.py": b"hit 3\n",
        "c.txt": b"hit 4\n",
    }.items():
        (tmp_path / name).write_bytes(raw)
        scope.data[name] = raw
    payload = request("search_text", query=r"hit \d", regex=True, glob="*.py", max_results=1)
    locations = []
    for _ in range(3):
        response = read_scoped_files(scope, payload, lambda: None)
        page = normalized(response)
        locations += [(match.path, match.line) for match in page.matches]
        payload["continuation"] = response.structured["continuation"]
    assert locations == [("a.py", 1), ("a.py", 3), ("b.py", 1)]
    assert payload["continuation"] is None
    assert all(call[1] != "c.txt" for call in scope.calls if call[0] == "read")
    assert all(call[2] is True for call in scope.calls if call[0] == "read")


def test_search_byte_budget_continuation_resumes_at_next_file(tmp_path, monkeypatch):
    from codito_agent import scoped_read_tools

    scope = FakeScope(tmp_path)
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"hit\n")
        scope.data[name] = b"hit\n"
    monkeypatch.setattr(scoped_read_tools, "MAX_SEARCH_SCANNED_BYTES", 4)
    payload = request("search_text", query="hit")
    first = read_scoped_files(scope, payload, lambda: None)
    second = read_scoped_files(
        scope, {**payload, "continuation": first.structured["continuation"]}, lambda: None
    )
    assert [item.path for item in normalized(first).matches] == ["a.txt"]
    assert [item.path for item in normalized(second).matches] == ["b.txt"]
    assert second.structured["continuation"] is None


@pytest.mark.parametrize("operation", ["list_directory", "read_file", "search_text"])
def test_cancelled_scope_never_enumerates_or_reads(tmp_path, operation):
    scope = FakeScope(tmp_path)

    def cancelled():
        raise AgentError("approval_expired", "Synthetic cancellation")

    with pytest.raises(AgentError, match="Synthetic cancellation"):
        read_scoped_files(scope, request(operation, path="file.txt", query="needle"), cancelled)
    assert not scope.calls


@pytest.mark.skipif(os.name != "nt", reason="Windows handle and hardlink validation")
def test_external_hardlink_is_not_read_or_searched(tmp_path):
    outside = tmp_path / "approved"
    outside.mkdir()
    original = tmp_path / "unapproved.txt"
    original.write_bytes(b"synthetic private line\n")
    os.link(original, outside / "alias.txt")
    with WindowsReadScope(str(outside)) as scope:
        with pytest.raises(AgentError, match="Hardlinked"):
            read_scoped_files(scope, request("read_file", path="alias.txt"), lambda: None)
        listing = read_scoped_files(scope, request("list_directory"), lambda: None)
        search = read_scoped_files(scope, request("search_text", query="private"), lambda: None)
    assert not normalized(listing).entries
    assert not normalized(search).matches


@pytest.mark.skipif(os.name != "nt", reason="Windows validated handle operations")
def test_real_nested_scope_read_uses_directory_pins_and_releases_data_handle(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    path = nested / "data.txt"
    path.write_bytes(b"synthetic\n")
    with WindowsReadScope(str(tmp_path)) as scope:
        response = read_scoped_files(
            scope, request("read_file", path="nested/data.txt"), lambda: None
        )
        assert normalized(response).sha256 == hashlib.sha256(b"synthetic\n").hexdigest()
        # The data handle is released, while directory mutation remains blocked.
        path.write_bytes(b"updated\n")
        with pytest.raises(OSError):
            nested.rename(tmp_path / "renamed")
