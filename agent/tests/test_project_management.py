from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from codito_protocol import validate_project_manage

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode
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
    project = database.register_project("Original", project_root, ProjectMode.NATIVE_APPROVAL)
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


@pytest.mark.parametrize("mode", list(ProjectMode))
@pytest.mark.parametrize("operation", ["rename_project", "remove_project"])
@pytest.mark.asyncio
async def test_project_metadata_uses_same_local_access_policy(
    tmp_path, project_root, mode, operation
):
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Original", project_root, mode)
    prompted = []
    changed = []

    async def approve(value):
        prompted.append(value)
        assert not value.persistent_read_eligible
        assert not value.persistent_shell_eligible
        assert not value.persistent_screen_eligible
        return ApprovalDecision.ALLOW_ONCE

    service = _service(database, ApprovalManager(approve), changed)
    request = validate_project_manage(
        {
            "operation": operation,
            "project_id": project.project_id,
            "idempotency_key": "metadata_abcdefghijkl",
            **({"title": "Renamed"} if operation == "rename_project" else {}),
        }
    )
    common = {
        "grant_id": "grant_abcdefghijkl",
        "link_id": "link_abcdefghijklmnop",
        "connection_epoch": 4,
        "deadline_at": datetime.now(UTC) + timedelta(minutes=1),
    }
    if mode is ProjectMode.ISOLATED:
        with pytest.raises(AgentError, match="sandbox_unavailable"):
            await service.execute(request, **common)
        assert prompted == changed == []
        assert database.get_project(project.project_id).enabled
        assert database.get_project(project.project_id).title == "Original"
        return
    result = await service.execute(request, **common)
    assert result.structured["status"] == (
        "renamed" if operation == "rename_project" else "removed"
    )
    assert len(prompted) == int(mode in {ProjectMode.NATIVE_APPROVAL, ProjectMode.NATIVE_TRUSTED})
    assert changed == [True]
    assert project_root.exists()  # Removing registration never deletes files.


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["mode", "disabled", "generation"])
async def test_metadata_approval_cannot_mutate_after_trust_changes(tmp_path, project_root, change):
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Original", project_root, ProjectMode.NATIVE_APPROVAL)
    opened = asyncio.Event()
    release = asyncio.Event()
    changed = []

    async def approve(_):
        opened.set()
        await release.wait()
        return ApprovalDecision.ALLOW_ONCE

    approvals = ApprovalManager(approve)
    service = _service(database, approvals, changed)
    task = asyncio.create_task(
        service.execute(
            validate_project_manage(
                {
                    "operation": "rename_project",
                    "project_id": project.project_id,
                    "title": "Must not be renamed",
                    "idempotency_key": "metadata_abcdefghijkl",
                }
            ),
            grant_id="grant_abcdefghijkl",
            link_id="link_abcdefghijklmnop",
            connection_epoch=4,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    )
    await opened.wait()
    if change == "mode":
        database.set_project_mode(project.project_id, ProjectMode.FULL_ACCESS)
    elif change == "disabled":
        database.set_project_enabled(project.project_id, enabled=False)
    else:
        approvals.clear("trust_change")
    release.set()
    with pytest.raises(AgentError, match=r"project_disabled|approval_expired"):
        await task
    assert changed == []
    assert database.get_project(project.project_id).title == "Original"
