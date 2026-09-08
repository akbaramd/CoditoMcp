from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from codito_protocol import validate_project_manage

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.db import AgentDatabase
from codito_agent.project_management import ProjectManagementService


def _service(
    database: AgentDatabase,
    approvals: ApprovalManager,
    changed: list[bool],
) -> ProjectManagementService:
    return ProjectManagementService(
        database,
        approvals,
        device_id="device_abcdefghijkl",
        account_id="account_abcdefghijkl",
        metadata_changed=lambda: changed.append(True),
    )


@pytest.mark.asyncio
async def test_remote_registration_requires_local_folder_selection(tmp_path: Path) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")

    async def no_prompt(_request):  # type: ignore[no-untyped-def]
        raise AssertionError("a registration request must not imply folder approval")

    service = _service(database, ApprovalManager(no_prompt), [])
    result = await service.execute(
        validate_project_manage(
            {
                "operation": "request_add_project",
                "title": "Requested project",
                "idempotency_key": "register_abcdefghijkl",
            }
        ),
        grant_id="grant_abcdefghijkl",
        link_id="link_abcdefghijklmnop",
        connection_epoch=3,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert result.structured["status"] == "pending_local_selection"
    pending = database.next_project_registration_request()
    assert pending is not None
    assert pending["title"] == "Requested project"
    assert "path" not in pending


@pytest.mark.asyncio
async def test_remote_rename_and_remove_are_one_shot_approved(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Original", project_root)
    prompts = []

    async def approve(request):  # type: ignore[no-untyped-def]
        prompts.append(request)
        return ApprovalDecision.ALLOW_ONCE

    changed: list[bool] = []
    service = _service(database, ApprovalManager(approve), changed)
    common = {
        "grant_id": "grant_abcdefghijkl",
        "link_id": "link_abcdefghijklmnop",
        "connection_epoch": 4,
        "deadline_at": datetime.now(UTC) + timedelta(minutes=1),
    }
    renamed = await service.execute(
        validate_project_manage(
            {
                "operation": "rename_project",
                "project_id": project.project_id,
                "title": "Renamed",
                "idempotency_key": "rename_abcdefghijkl",
            }
        ),
        **common,
    )
    assert renamed.structured["status"] == "renamed"
    assert database.get_project(project.project_id).title == "Renamed"

    removed = await service.execute(
        validate_project_manage(
            {
                "operation": "remove_project",
                "project_id": project.project_id,
                "idempotency_key": "remove_abcdefghijkl",
            }
        ),
        **common,
    )
    assert removed.structured["status"] == "removed"
    assert not database.get_project(project.project_id).enabled
    assert len(prompts) == 2
    assert all(not prompt.session_eligible for prompt in prompts)
    assert len(changed) == 2
