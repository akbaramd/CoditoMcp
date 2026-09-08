from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_approvals import request

from codito_agent.access_policy import authorize_project_operation, project_access_policy
from codito_agent.approvals import ApprovalDecision, ApprovalManager, ApprovalRisk
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.models import Project, ProjectMode


def project(mode):
    return Project(
        "project_abcdefghijkl", "Policy project", Path("D:/project"), "root-identity", mode
    )


@pytest.mark.parametrize("inside,uncertain", [(True, False), (True, True), (False, False)])
@pytest.mark.parametrize("mode", list(ProjectMode))
def test_access_modes_never_reinterpret_legacy_native_trust(mode, inside, uncertain):
    if mode is ProjectMode.ISOLATED:
        with pytest.raises(AgentError, match="sandbox_unavailable"):
            project_access_policy(project(mode), inside_project=inside, uncertain=uncertain)
        return
    policy = project_access_policy(project(mode), inside_project=inside, uncertain=uncertain)
    if mode is ProjectMode.FULL_ACCESS:
        assert not policy.requires_approval
        assert not policy.allow_saved_permissions
    elif mode in {ProjectMode.NATIVE_APPROVAL, ProjectMode.NATIVE_TRUSTED}:
        assert policy.requires_approval
        assert not policy.allow_saved_permissions
    else:
        assert policy.requires_approval == (not inside or uncertain)
        assert policy.allow_saved_permissions


def test_disabled_full_access_is_still_denied():
    with pytest.raises(AgentError, match="project_disabled"):
        project_access_policy(
            replace(project(ProjectMode.FULL_ACCESS), enabled=False), inside_project=False
        )


@pytest.mark.asyncio
async def test_ask_every_operation_ignores_but_does_not_delete_saved_permissions(tmp_path):
    database = AgentDatabase(tmp_path / "policy.sqlite")
    prompts = []

    async def prompt(value):
        prompts.append(value)
        return (
            ApprovalDecision.ALLOW_ALWAYS_READ if len(prompts) == 1 else ApprovalDecision.ALLOW_ONCE
        )

    approvals = ApprovalManager(prompt, database)
    value = replace(
        request(),
        capability="device:read",
        risk=ApprovalRisk.READ,
        requested_external_paths=("C:\\",),
    )
    scope = ("C:\\", "drive-identity")
    await authorize_project_operation(
        project(ProjectMode.NATIVE_PROJECT),
        approvals,
        value,
        inside_project=False,
        read_scope=scope,
    )
    await authorize_project_operation(
        project(ProjectMode.NATIVE_PROJECT),
        approvals,
        value,
        inside_project=False,
        read_scope=scope,
    )
    assert len(prompts) == 1
    for _ in range(2):
        await authorize_project_operation(
            project(ProjectMode.NATIVE_APPROVAL),
            approvals,
            value,
            inside_project=False,
            read_scope=scope,
        )
    assert len(prompts) == 3
    assert not prompts[-1].persistent_read_eligible
    assert len(database.list_read_permissions()) == 1


@pytest.mark.asyncio
async def test_saved_read_permission_cannot_cross_origin_projects(tmp_path):
    prompts = []

    async def prompt(value):
        prompts.append(value)
        return ApprovalDecision.ALLOW_ALWAYS_READ

    manager = ApprovalManager(prompt, AgentDatabase(tmp_path / "policy.sqlite"))
    value = replace(
        request(),
        capability="device:read",
        risk=ApprovalRisk.READ,
        requested_external_paths=("C:\\",),
    )
    for selected in (value, value, replace(value, project_id="other-project")):
        await manager.request(selected, session_eligible=False, read_scope=("C:\\", "drive"))
    assert len(prompts) == 2


@pytest.mark.asyncio
async def test_saved_shell_digest_prevents_reusing_different_command(tmp_path):
    prompts = []

    async def prompt(value):
        prompts.append(value)
        return ApprovalDecision.ALLOW_ALWAYS_SHELL

    manager = ApprovalManager(prompt, AgentDatabase(tmp_path / "policy.sqlite"))
    value = replace(request(), command={"kind": "exec", "executable": "uv"})
    for selected in (value, value, replace(value, action_digest="b" * 64)):
        await manager.request(
            selected, session_eligible=False, shell_scope=("same-scope", "same-root")
        )
    assert len(prompts) == 2


@pytest.mark.asyncio
async def test_full_access_skips_local_prompt_not_deadline_or_disabled_check():
    async def prompt(_value):
        pytest.fail("Explicit full access has no local prompts")

    approvals = ApprovalManager(prompt)
    await authorize_project_operation(
        project(ProjectMode.FULL_ACCESS),
        approvals,
        request(),
        inside_project=False,
        uncertain=True,
    )
    with pytest.raises(AgentError, match="project_disabled"):
        await authorize_project_operation(
            replace(project(ProjectMode.FULL_ACCESS), enabled=False),
            approvals,
            request(),
            inside_project=False,
        )
    with pytest.raises(AgentError, match="approval_expired"):
        await authorize_project_operation(
            project(ProjectMode.FULL_ACCESS),
            approvals,
            replace(request(), deadline_at=datetime.now(UTC) - timedelta(seconds=1)),
            inside_project=False,
        )
