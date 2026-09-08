from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.protocol_adapter import AgentProtocolAdapter
from codito_agent.read_tools import ToolResponse


class ConcurrentReadService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum = 0

    def execute(self, request: dict[str, Any]) -> ToolResponse:
        with self._lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
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


@pytest.mark.asyncio
async def test_adapter_enforces_configured_read_concurrency() -> None:
    async def deny(_: Any) -> ApprovalDecision:
        raise AssertionError("reads must not prompt")

    reads = ConcurrentReadService()
    adapter = AgentProtocolAdapter(
        database=object(),  # type: ignore[arg-type]
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
        "project_id": "project_abcdefghijkl",
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
