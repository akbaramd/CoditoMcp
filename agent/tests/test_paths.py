from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from codito_agent.errors import AgentError
from codito_agent.models import Project
from codito_agent.paths import (
    ProjectPathResolver,
    _assert_fixed_drive,
    _assert_supported_filesystem,
    _windows_attributes,
    root_fingerprint,
    validate_project_root,
    validate_relative_path,
)


@pytest.mark.parametrize(
    "value",
    [
        "../secret",
        "a/../secret",
        r"C:\Windows\win.ini",
        r"C:relative",
        r"\\server\share\file",
        r"\\?\C:\file",
        "file.txt:stream",
        "CON.txt",
        "dir/NUL",
        "trailing. ",
        "a//b",
        "a/./b",
        "*.txt",
    ],
)
def test_relative_path_rejects_windows_escape_aliases(value: str) -> None:
    with pytest.raises(AgentError):
        validate_relative_path(value)


@given(st.text(alphabet="abcXYZ019_-.", min_size=1, max_size=40))
def test_valid_single_components_never_become_absolute(value: str) -> None:
    if value in {".", ".."} or value.endswith((".", " ")):
        return
    try:
        normalized = validate_relative_path(value)
    except AgentError:
        return
    assert "/" not in normalized
    assert not Path(normalized).is_absolute()


def test_resolver_rejects_hardlink_mutation(project_root: Path) -> None:
    source = project_root / "source.txt"
    source.write_text("value", encoding="utf-8")
    link = project_root / "link.txt"
    try:
        link.hardlink_to(source)
    except OSError:
        pytest.skip("hard links unavailable on this test filesystem")
    project = Project(
        "project_abcdefghijkl", "Project", project_root, root_fingerprint(project_root)
    )
    with pytest.raises(AgentError, match="multiply-linked"):
        ProjectPathResolver().resolve(project, "source.txt", for_write=True)


def test_resolver_rejects_symlink_traversal(project_root: Path) -> None:
    target = project_root / "target"
    target.mkdir()
    link = project_root / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires a Windows developer setting")
    project = Project(
        "project_abcdefghijkl", "Project", project_root, root_fingerprint(project_root)
    )
    with pytest.raises(AgentError, match="Reparse"):
        ProjectPathResolver().resolve(project, "link", directory=True)


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows kernel32 path")
def test_windows_root_validation_calls_attribute_and_drive_apis(project_root: Path) -> None:
    assert isinstance(_windows_attributes(project_root), int)
    _assert_fixed_drive(project_root)
    _assert_supported_filesystem(project_root)
    resolved, fingerprint = validate_project_root(project_root)
    assert resolved == project_root.resolve(strict=True)
    assert fingerprint == root_fingerprint(project_root)
