from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from codito_agent.approvals import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
    ApprovalRisk,
)
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode


def request() -> ApprovalRequest:
    return ApprovalRequest(
        "request_abcdefghijkl",
        "account_abcdefghijkl",
        "grant_abcdefghijklmn",
        "link_abcdefghijklmnop",
        "device_abcdefghijkl",
        "project_abcdefghijkl",
        "Project",
        "shell:execute",
        "a" * 64,
        1,
        datetime.now(UTC) + timedelta(minutes=1),
        ApprovalRisk.NATIVE_EXECUTION,
        "Run tests",
    )


@pytest.mark.asyncio
async def test_native_approval_is_one_shot() -> None:
    calls = 0

    async def prompt(value: ApprovalRequest) -> ApprovalDecision:
        nonlocal calls
        calls += 1
        return ApprovalDecision.ALLOW_ONCE

    manager = ApprovalManager(prompt)
    await manager.authorize_shell(
        mode=ProjectMode.NATIVE_APPROVAL, sandbox_proven=False, request=request()
    )
    await manager.authorize_shell(
        mode=ProjectMode.NATIVE_APPROVAL, sandbox_proven=False, request=request()
    )
    assert calls == 2


@pytest.mark.asyncio
async def test_isolated_mode_fails_closed_without_proof() -> None:
    async def prompt(value: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.ALLOW_ONCE

    with pytest.raises(AgentError) as error:
        await ApprovalManager(prompt).authorize_shell(
            mode=ProjectMode.ISOLATED, sandbox_proven=False, request=request()
        )
    assert error.value.code == "sandbox_unavailable"


@pytest.mark.asyncio
async def test_pending_approval_expires_at_operation_deadline() -> None:
    async def prompt(_: ApprovalRequest) -> ApprovalDecision:
        await asyncio.sleep(1)
        return ApprovalDecision.ALLOW_ONCE

    expiring = replace(request(), deadline_at=datetime.now(UTC) + timedelta(milliseconds=20))
    with pytest.raises(AgentError) as error:
        await ApprovalManager(prompt).request(expiring, session_eligible=False)
    assert error.value.code == "approval_expired"


@pytest.mark.asyncio
async def test_pending_approval_is_invalidated_by_disconnect() -> None:
    opened = asyncio.Event()
    release = asyncio.Event()

    async def prompt(_: ApprovalRequest) -> ApprovalDecision:
        opened.set()
        await release.wait()
        return ApprovalDecision.ALLOW_ONCE

    manager = ApprovalManager(prompt)
    task = asyncio.create_task(manager.request(request(), session_eligible=False))
    await opened.wait()
    manager.invalidate_pending("relay_disconnect")
    release.set()
    with pytest.raises(AgentError) as error:
        await task
    assert error.value.code == "approval_expired"


@pytest.mark.asyncio
async def test_session_grant_is_scoped_to_device_link() -> None:
    calls = 0

    async def prompt(_: ApprovalRequest) -> ApprovalDecision:
        nonlocal calls
        calls += 1
        return ApprovalDecision.ALLOW_SESSION

    manager = ApprovalManager(prompt)
    original = request()
    await manager.request(original, session_eligible=True)
    await manager.request(original, session_eligible=True)
    await manager.request(replace(original, link_id="link_different12345"), session_eligible=True)
    assert calls == 2
