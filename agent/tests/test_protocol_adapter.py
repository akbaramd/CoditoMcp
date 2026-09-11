from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from codito_protocol.code import (
    CodeIntelligenceStatusResult,
    CodeReindexResult,
    Precision,
    ProviderKind,
    WorkspaceReadyState,
)

from codito_agent.approvals import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
    ApprovalRisk,
)
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode
from codito_agent.protocol_adapter import AgentProtocolAdapter
from codito_agent.read_tools import ToolResponse


class ConcurrentReadService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.active_by_project: dict[str, int] = {}
        self.maximum_by_project: dict[str, int] = {}

    def execute(self, request: dict[str, Any]) -> ToolResponse:
        with self._lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            project_id = request["project_id"]
            self.active_by_project[project_id] = self.active_by_project.get(project_id, 0) + 1
            self.maximum_by_project[project_id] = max(
                self.maximum_by_project.get(project_id, 0), self.active_by_project[project_id]
            )
        try:
            time.sleep(0.05)
            return ToolResponse(
                {
                    "operation": "read_file",
                    "project_id": request["project_id"],
                    "path": "file.txt",
                    "text": "value\n",
                    "numbered_text": "     1 | value\n",
                    "encoding": "utf-8",
                    "newline": "lf",
                    "size": 6,
                    "sha256": "a" * 64,
                    "first_line": 1,
                    "last_line": 1,
                    "truncated": False,
                    "continuation": None,
                },
                "Read value.",
            )
        finally:
            with self._lock:
                self.active -= 1
                self.active_by_project[project_id] -= 1


@pytest.mark.asyncio
async def test_adapter_enforces_configured_read_concurrency(
    tmp_path: Path, project_root: Path
) -> None:
    async def deny(_: Any) -> ApprovalDecision:
        raise AssertionError("reads must not prompt")

    reads = ConcurrentReadService()
    database = AgentDatabase(tmp_path / "reads.sqlite")
    project = database.register_project(
        "Read concurrency", project_root, ProjectMode.NATIVE_PROJECT
    )
    adapter = AgentProtocolAdapter(
        database=database,
        reads=reads,  # type: ignore[arg-type]
        patches=object(),  # type: ignore[arg-type]
        shells=object(),  # type: ignore[arg-type]
        device_id="device_abcdefghijkl",
        account_id="account_abcdefghijkl",
        approvals=ApprovalManager(deny),
        read_concurrency=2,
    )
    request = {
        "operation": "read_file",
        "project_id": project.project_id,
        "path": "file.txt",
    }
    await asyncio.gather(
        *(
            adapter.execute(
                "project_read",
                request,
                grant_id="grant_abcdefghijkl",
                link_id="link_abcdefghijklmnop",
                connection_epoch=1,
                deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            for _ in range(6)
        )
    )
    assert reads.maximum == 2


@pytest.mark.asyncio
async def test_read_capacity_is_partitioned_across_projects(
    tmp_path: Path, project_root: Path
) -> None:
    async def deny(_: Any) -> ApprovalDecision:
        raise AssertionError("reads must not prompt")

    reads = ConcurrentReadService()
    database = AgentDatabase(tmp_path / "fair-reads.sqlite")
    first = database.register_project("First reads", project_root, ProjectMode.NATIVE_PROJECT)
    second_root = tmp_path / "second-reads"
    second_root.mkdir()
    second = database.register_project("Second reads", second_root, ProjectMode.NATIVE_PROJECT)
    adapter = AgentProtocolAdapter(
        database=database,
        reads=reads,  # type: ignore[arg-type]
        patches=object(),  # type: ignore[arg-type]
        shells=object(),  # type: ignore[arg-type]
        device_id="device_abcdefghijkl",
        account_id="account_abcdefghijkl",
        approvals=ApprovalManager(deny),
        read_concurrency=4,
        read_concurrency_per_project=2,
    )

    async def read(project_id: str):
        return await adapter.execute(
            "project_read",
            {"operation": "read_file", "project_id": project_id, "path": "file.txt"},
            grant_id="grant_abcdefghijkl",
            link_id="link_abcdefghijklmnop",
            connection_epoch=1,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        )

    await asyncio.gather(
        *(read(first.project_id) for _ in range(6)),
        *(read(second.project_id) for _ in range(6)),
    )

    assert reads.maximum == 4
    assert reads.maximum_by_project == {first.project_id: 2, second.project_id: 2}


@pytest.mark.asyncio
async def test_reconciled_patch_result_returns_before_destructive_reapproval() -> None:
    async def must_not_prompt(_: Any) -> ApprovalDecision:
        raise AssertionError("a durably committed patch must not prompt or run again")

    class ReconciledPatchService:
        def reconcile_duplicate(self, **values: Any) -> ToolResponse:
            return ToolResponse(
                {
                    "project_id": values["project_id"],
                    "idempotency_key": values["idempotency_key"],
                    "dry_run": False,
                    "applied": True,
                    "files": [
                        {
                            "operation": "delete",
                            "path": "old.txt",
                            "old_sha256": "a" * 64,
                        }
                    ],
                    "conflicts": [],
                    "journal_id": "journal_abcdefghijkl",
                },
                "Recovered committed result.",
            )

        def apply(self, **values: Any) -> ToolResponse:
            raise AssertionError("committed patch must not be applied again")

    adapter = AgentProtocolAdapter(
        database=object(),  # type: ignore[arg-type]
        reads=object(),  # type: ignore[arg-type]
        patches=ReconciledPatchService(),  # type: ignore[arg-type]
        shells=object(),  # type: ignore[arg-type]
        device_id="device_abcdefghijkl",
        account_id="account_abcdefghijkl",
        approvals=ApprovalManager(must_not_prompt),
    )
    result = await adapter.execute(
        "project_apply_patch",
        {
            "project_id": "project_abcdefghijkl",
            "patch": "*** Begin Patch\n*** Delete File: old.txt\n*** End Patch",
            "base_hashes": {"old.txt": "a" * 64},
            "idempotency_key": "patch_reconnect_key",
            "dry_run": False,
        },
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
        connection_epoch=2,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        reconcile_duplicate=True,
    )
    assert result.structured["applied"] is True


class ImmediateCodeIntelligence:
    async def execute(
        self, request: Any, project_root: Path, *, root_identity: str | None = None
    ) -> CodeIntelligenceStatusResult | CodeReindexResult:
        del project_root, root_identity
        captured_at = datetime.now(UTC).isoformat()
        if request.operation == "code_intelligence_status":
            return CodeIntelligenceStatusResult(
                snapshot_id="snapshot_adapter_0001",
                provider=ProviderKind.MANIFEST,
                precision=Precision.STRUCTURAL,
                captured_at=captured_at,
                status=WorkspaceReadyState.READY,
                indexed_files=1,
            )
        assert request.operation == "code_reindex"
        return CodeReindexResult(
            snapshot_id="snapshot_adapter_0002",
            provider=ProviderKind.GRAPH,
            precision=Precision.STRUCTURAL,
            captured_at=captured_at,
            completed=True,
            generation="snapshot_adapter_0002",
            files_processed=1,
        )


class BlockingCodeIntelligence:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(
        self, request: Any, project_root: Path, *, root_identity: str | None = None
    ) -> Any:
        del request, project_root, root_identity
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _code_adapter(
    database: AgentDatabase,
    approvals: ApprovalManager,
    code_intelligence: Any,
) -> AgentProtocolAdapter:
    return AgentProtocolAdapter(
        database=database,
        reads=object(),  # type: ignore[arg-type]
        patches=object(),  # type: ignore[arg-type]
        shells=object(),  # type: ignore[arg-type]
        device_id="device_abcdefghijkl",
        account_id="account_abcdefghijkl",
        approvals=approvals,
        code_intelligence=code_intelligence,
    )


@pytest.mark.asyncio
async def test_code_status_is_metadata_read_and_does_not_prompt(
    tmp_path: Path, project_root: Path
) -> None:
    async def must_not_prompt(_: ApprovalRequest) -> ApprovalDecision:
        raise AssertionError("code status must not request native execution consent")

    database = AgentDatabase(tmp_path / "code-status.sqlite")
    project = database.register_project("Code status", project_root, ProjectMode.NATIVE_PROJECT)
    adapter = _code_adapter(
        database,
        ApprovalManager(must_not_prompt),
        ImmediateCodeIntelligence(),
    )
    result = await adapter.execute(
        "project_code",
        {"operation": "code_intelligence_status", "project_id": project.project_id},
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
        connection_epoch=1,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert result.structured["operation"] == "code_intelligence_status"
    assert result.structured["status"] == "ready"


@pytest.mark.asyncio
async def test_code_reindex_requests_native_execution_consent(
    tmp_path: Path, project_root: Path
) -> None:
    prompts: list[ApprovalRequest] = []

    async def allow_once(request: ApprovalRequest) -> ApprovalDecision:
        prompts.append(request)
        return ApprovalDecision.ALLOW_ONCE

    database = AgentDatabase(tmp_path / "code-native.sqlite")
    project = database.register_project("Code native", project_root, ProjectMode.NATIVE_PROJECT)
    adapter = _code_adapter(
        database,
        ApprovalManager(allow_once),
        ImmediateCodeIntelligence(),
    )
    result = await adapter.execute(
        "project_code",
        {
            "operation": "code_reindex",
            "project_id": project.project_id,
            "purpose": "Refresh semantic and structural analysis",
            "scope": "incremental",
            "timeout_seconds": 5,
        },
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
        connection_epoch=2,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert result.structured["completed"] is True
    assert len(prompts) == 1
    assert prompts[0].capability == "shell:execute"
    assert prompts[0].risk is ApprovalRisk.NATIVE_EXECUTION


@pytest.mark.asyncio
async def test_revoking_native_permission_cancels_running_code_analysis(
    tmp_path: Path, project_root: Path
) -> None:
    async def allow_once(_: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.ALLOW_ONCE

    database = AgentDatabase(tmp_path / "code-revoke.sqlite")
    project = database.register_project("Code revoke", project_root, ProjectMode.NATIVE_PROJECT)
    approvals = ApprovalManager(allow_once)
    code = BlockingCodeIntelligence()
    adapter = _code_adapter(database, approvals, code)
    task = asyncio.create_task(
        adapter.execute(
            "project_code",
            {
                "operation": "code_reindex",
                "project_id": project.project_id,
                "purpose": "Long-running analysis",
                "scope": "incremental",
                "timeout_seconds": 5,
            },
            grant_id="grant_abcdefghijklmn",
            link_id="link_abcdefghijklmnop",
            connection_epoch=3,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    )
    await asyncio.wait_for(code.started.wait(), timeout=2)
    approvals.revoke_shell_permissions()
    with pytest.raises(AgentError) as raised:
        await asyncio.wait_for(task, timeout=2)
    assert raised.value.code == "approval_expired"
    assert code.cancelled.is_set()
