from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from codito_protocol.code import (
    CodeLocation,
    CodeReferencesInput,
    CodeReferencesResult,
    CodeReindexInput,
    CodeReindexResult,
    CodeWorkspaceSummaryInput,
    CodeWorkspaceSummaryResult,
    Precision,
    ProviderKind,
    ReindexScope,
)

from codito_agent.code_intelligence import manager as manager_module
from codito_agent.code_intelligence.cbm_provider import (
    CodebaseMemoryGraphProvider,
    IndexInfo,
)
from codito_agent.code_intelligence.manager import CodeIntelligenceManager
from codito_agent.code_intelligence.types import CapabilitySet, WorkspaceSnapshot
from codito_agent.errors import AgentError

PROJECT_ID = "project_ci_manager_0001"


class FakeGraphProvider(CodebaseMemoryGraphProvider):
    instances: ClassVar[list[FakeGraphProvider]] = []

    def __init__(self, root: Path, data_directory: Path, executable: Path | None = None) -> None:
        del data_directory, executable
        self.root = root
        self.index_calls: list[tuple[str, bool, set[str]]] = []
        self._snapshot: WorkspaceSnapshot | None = None
        self.__class__.instances.append(self)

    @property
    def available(self) -> bool:
        return True

    async def ensure_indexed(
        self,
        *,
        snapshot: WorkspaceSnapshot | None = None,
        full_rebuild: bool = False,
    ) -> IndexInfo:
        assert snapshot is not None
        self._snapshot = snapshot
        self.index_calls.append((snapshot.generation, full_rebuild, set(snapshot.file_states)))
        return IndexInfo("fake-graph", nodes=len(snapshot.file_states), edges=0)

    async def references(
        self,
        path: str,
        line: int,
        include_declaration: bool,
        max_results: int,
        snapshot_id: str,
    ) -> CodeReferencesResult:
        del line, include_declaration, max_results
        return CodeReferencesResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.GRAPH,
            precision=Precision.STRUCTURAL,
            captured_at=datetime.now(UTC).isoformat(),
            references=[CodeLocation(path=path)],
            total_matches=1,
            warnings=["fake structural reference evidence"],
        )


@pytest.fixture
def fake_graph(monkeypatch: pytest.MonkeyPatch) -> type[FakeGraphProvider]:
    FakeGraphProvider.instances.clear()
    monkeypatch.setattr(manager_module, "CodebaseMemoryGraphProvider", FakeGraphProvider)
    return FakeGraphProvider


@pytest.mark.asyncio
async def test_external_add_rename_delete_are_visible_without_manual_reindex(
    tmp_path: Path, fake_graph: type[FakeGraphProvider]
) -> None:
    del fake_graph
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    manager = CodeIntelligenceManager(tmp_path / ".codito-data")
    request = CodeWorkspaceSummaryInput(project_id=PROJECT_ID)
    try:
        first = await manager.execute(request, tmp_path)
        assert isinstance(first, CodeWorkspaceSummaryResult)
        assert first.total_files == 1

        (tmp_path / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        second = await manager.execute(request, tmp_path)
        assert isinstance(second, CodeWorkspaceSummaryResult)
        assert second.total_files == 2
        snapshot = manager._workspaces[PROJECT_ID].snapshot
        assert snapshot is not None
        assert "b.py" in snapshot.file_states

        (tmp_path / "a.py").rename(tmp_path / "c.py")
        third = await manager.execute(request, tmp_path)
        assert isinstance(third, CodeWorkspaceSummaryResult)
        assert third.total_files == 2
        snapshot = manager._workspaces[PROJECT_ID].snapshot
        assert snapshot is not None
        paths = snapshot.file_states
        assert "a.py" not in paths and "c.py" in paths

        (tmp_path / "b.py").unlink()
        fourth = await manager.execute(request, tmp_path)
        assert isinstance(fourth, CodeWorkspaceSummaryResult)
        assert fourth.total_files == 1
        snapshot = manager._workspaces[PROJECT_ID].snapshot
        assert snapshot is not None
        assert set(snapshot.file_states) == {"c.py"}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_incremental_and_full_reindex_refresh_the_current_snapshot(
    tmp_path: Path, fake_graph: type[FakeGraphProvider]
) -> None:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    manager = CodeIntelligenceManager(tmp_path / ".codito-data")
    try:
        incremental = await manager.execute(
            CodeReindexInput(
                project_id=PROJECT_ID,
                purpose="Refresh changed code",
                scope=ReindexScope.INCREMENTAL,
                timeout_seconds=5,
            ),
            tmp_path,
        )
        assert isinstance(incremental, CodeReindexResult)
        assert incremental.completed and not incremental.timed_out
        provider = fake_graph.instances[-1]
        assert provider.index_calls[-1][1] is False
        assert provider.index_calls[-1][2] == {"a.py"}

        (tmp_path / "new.py").write_text("def new():\n    return 2\n", encoding="utf-8")
        full = await manager.execute(
            CodeReindexInput(
                project_id=PROJECT_ID,
                purpose="Rebuild code intelligence",
                scope=ReindexScope.FULL,
                timeout_seconds=5,
            ),
            tmp_path,
        )
        assert isinstance(full, CodeReindexResult)
        assert full.completed and not full.timed_out
        full_call = provider.index_calls[-1]
        assert full_call[1]
        assert full_call[2] == {"a.py", "new.py"}
        snapshot = manager._workspaces[PROJECT_ID].snapshot
        assert snapshot is not None
        assert full.generation == snapshot.generation
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_repeated_query_race_returns_explicit_retryable_error(
    tmp_path: Path, fake_graph: type[FakeGraphProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_graph
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    manager = CodeIntelligenceManager(tmp_path / ".codito-data")
    request = CodeWorkspaceSummaryInput(project_id=PROJECT_ID)
    try:
        await manager.execute(request, tmp_path)
        workspace = manager._workspaces[PROJECT_ID]

        async def never_current(snapshot: WorkspaceSnapshot) -> bool:
            del snapshot
            return False

        monkeypatch.setattr(workspace, "snapshot_is_current", never_current)
        with pytest.raises(AgentError) as raised:
            await manager.execute(request, tmp_path)
        assert raised.value.code == "path_race"
        assert raised.value.retryable is True
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_workspace_identity_cannot_change_under_same_project_id(
    tmp_path: Path, fake_graph: type[FakeGraphProvider]
) -> None:
    del fake_graph
    (tmp_path / "a.py").write_text("def a(): pass\n", encoding="utf-8")
    manager = CodeIntelligenceManager(tmp_path / ".codito-data")
    request = CodeWorkspaceSummaryInput(project_id=PROJECT_ID)
    try:
        await manager.execute(request, tmp_path)
        with pytest.raises(AgentError) as raised:
            await manager.execute(request, tmp_path, root_identity="different_root_identity")
        assert raised.value.code == "project_identity_changed"
    finally:
        await manager.close()


class EmptyReferenceLsp:
    capabilities = CapabilitySet(references=True)

    async def references(self, *args: Any, **kwargs: Any) -> CodeReferencesResult:
        snapshot_id = str(args[-1])
        return CodeReferencesResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.LSP,
            precision=Precision.EXACT,
            captured_at=datetime.now(UTC).isoformat(),
            references=[],
            total_matches=0,
        )


@pytest.mark.asyncio
async def test_empty_semantic_references_are_cross_checked_by_structural_graph(
    tmp_path: Path, fake_graph: type[FakeGraphProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    manager = CodeIntelligenceManager(tmp_path / ".codito-data")
    try:
        await manager.execute(CodeWorkspaceSummaryInput(project_id=PROJECT_ID), tmp_path)
        workspace = manager._workspaces[PROJECT_ID]
        await workspace.force_reindex(full=False)

        @asynccontextmanager
        async def empty_lsp(path: str) -> AsyncIterator[EmptyReferenceLsp]:
            del path
            yield EmptyReferenceLsp()

        monkeypatch.setattr(workspace, "lsp_for_document", empty_lsp)
        result = await manager.execute(
            CodeReferencesInput(
                project_id=PROJECT_ID,
                purpose="Cross-check references",
                path="a.py",
                line=1,
                character=5,
            ),
            tmp_path,
        )
        assert isinstance(result, CodeReferencesResult)
        assert result.provider == ProviderKind.GRAPH
        assert result.precision == Precision.STRUCTURAL
        assert result.references == [CodeLocation(path="a.py")]
        assert any("Semantic provider returned no references" in item for item in result.warnings)
        assert fake_graph.instances[-1].index_calls
    finally:
        await manager.close()
