from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path() -> Path:
    """Workspace-local temp path avoids Windows sandbox ACL rewriting by pytest."""

    parent = Path(__file__).parent / ".test-work"
    parent.mkdir(exist_ok=True)
    path = parent / f"case-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root
