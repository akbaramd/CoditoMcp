import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from test_shell import FakeBroker, FakeProcess

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.db import AgentDatabase
from codito_agent.models import ProjectMode
from codito_agent.paths import ProjectPathResolver
from codito_agent.shell import ShellManager
from codito_agent.shell_policy import outside_references


@pytest.mark.parametrize(
    "command,expected",
    [
        ({"kind": "exec", "executable": "uv", "arguments": ["run", "pytest"]}, False),
        ({"kind": "script", "script": "Get-ChildItem C:\\"}, True),
        ({"kind": "script", "script": "Get-ChildItem 'D:\\project\\files'"}, False),
        ({"kind": "exec", "arguments": ["../private"]}, True),
        ({"kind": "script", "script": "dir D:\\project2"}, True),
        ({"kind": "script", "script": "dir 'D:\\project\\..\\private'"}, True),
    ],
)
def test_literal_reference_routing(command, expected):
    assert bool(outside_references(r"D:\project", r"D:\project", command, [])) == expected


def manager_for(tmp_path, project_root, prompt):
    database = AgentDatabase(tmp_path / "shell.sqlite3")
    project = database.register_project("Test", project_root, ProjectMode.NATIVE_PROJECT)
    started = []

    async def start(spec, isolated):
        assert not isolated
        started.append(spec)
        return FakeProcess()

    manager = ShellManager(
        database,
        ProjectPathResolver(),
        FakeBroker(),
        ApprovalManager(prompt, database),
        tmp_path,
        account_id="account",
        device_id="device",
        process_starter=start,
    )
    return manager, project, started


async def start_job(manager, project, **extra):
    return await manager.start(
        {
            "project_id": project.project_id,
            "purpose": "Synthetic shell policy test",
            "idempotency_key": "project_policy_key",
            "command": {"kind": "exec", "executable": "uv"},
            **extra,
        },
        grant_id="grant",
        link_id="link",
        connection_epoch=1,
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_project_command_has_no_prompt(tmp_path, project_root):
    async def prompt(_):
        raise AssertionError("Inside project needs no approval")

    manager, project, started = manager_for(tmp_path, project_root, prompt)
    result = await start_job(manager, project)
    job = manager._jobs[result.structured["job_id"]]
    await job.task
    assert job.state == "completed"
    assert len(started) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [ApprovalDecision.DENY, ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_ALWAYS_SHELL],
)
async def test_external_literal_waits_and_decision_controls_execution(
    tmp_path, project_root, decision
):
    release = asyncio.Event()
    opened = asyncio.Event()
    prompted = []

    async def prompt(value):
        prompted.append(value)
        opened.set()
        await release.wait()
        return decision

    manager, project, started = manager_for(tmp_path, project_root, prompt)
    command = {"kind": "script", "shell": "powershell", "script": "Get-ChildItem C:\\"}
    response = await asyncio.wait_for(start_job(manager, project, command=command), 1)
    job = manager._jobs[response.structured["job_id"]]
    assert response.structured["state"] == "pending_approval"
    await opened.wait()
    assert started == []
    assert (prompted[0].deadline_at - datetime.now(UTC)).total_seconds() > 150
    duplicate = await start_job(manager, project, command=command)
    assert duplicate.structured["job_id"] == job.job_id
    release.set()
    await job.task
    assert job.state == ("failed" if decision is ApprovalDecision.DENY else "completed")
    assert len(started) == (0 if decision is ApprovalDecision.DENY else 1)
    if decision is ApprovalDecision.ALLOW_ALWAYS_SHELL:
        again = await start_job(manager, project, command=command, idempotency_key="another_key")
        await manager._jobs[again.structured["job_id"]].task
        assert len(started) == 2
        assert len(prompted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["cancel", "disconnect", "revoke"])
async def test_pending_job_cannot_start_after_invalidation(tmp_path, project_root, invalidate):
    release = asyncio.Event()
    opened = asyncio.Event()

    async def prompt(_):
        opened.set()
        await release.wait()
        return ApprovalDecision.ALLOW_ONCE

    manager, project, started = manager_for(tmp_path, project_root, prompt)
    response = await start_job(manager, project, requested_external_paths=["C:/"])
    job = manager._jobs[response.structured["job_id"]]
    await opened.wait()
    if invalidate == "cancel":
        await manager.cancel(project.project_id, job.job_id, grant_id="grant", link_id="link")
    elif invalidate == "disconnect":
        manager.disconnected()
        await asyncio.sleep(0)
    else:
        manager.approvals.revoke_shell_permissions()
    release.set()
    await asyncio.gather(job.task, return_exceptions=True)
    assert started == []
    assert job.state in {"cancelled", "failed"}
    # Project lock released, even when a pending prompt was cancelled.
    result = await start_job(manager, project, idempotency_key="next_inside")
    await manager._jobs[result.structured["job_id"]].task
    assert len(started) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows handle validation")
@pytest.mark.asyncio
async def test_external_cwd_is_passed_only_after_native_consent(tmp_path, project_root):
    async def prompt(value):
        assert value.working_directory == str(tmp_path)
        return ApprovalDecision.ALLOW_ONCE

    manager, project, started = manager_for(tmp_path, project_root, prompt)
    response = await start_job(manager, project, external_working_directory=str(tmp_path))
    await manager._jobs[response.structured["job_id"]].task
    assert len(started) == 1
    assert started[0]["external_working_directory_authorized"] is True
