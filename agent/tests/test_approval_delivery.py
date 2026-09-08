import asyncio
from dataclasses import replace

import pytest
from test_approvals import request

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.ipc import QueuedApprovalPrompt


@pytest.mark.asyncio
async def test_delivery_survives_tray_crash_and_duplicates():
    queue = QueuedApprovalPrompt()
    task = asyncio.create_task(queue(request()))
    await asyncio.sleep(0)
    first = queue.next_request()
    assert queue.next_request() == first  # UI died before displaying, not lost.
    assert queue.mark_displayed(first["request_id"])
    assert queue.next_request() == first  # Restarted tray can recover it.
    assert queue.respond(first["request_id"], "allow_once")
    assert queue.respond(first["request_id"], "deny")  # callback race is harmless
    assert await task is ApprovalDecision.ALLOW_ONCE
    assert queue.next_request() is None
    assert not queue.respond(first["request_id"], "allow_once")


@pytest.mark.asyncio
async def test_cancelled_first_request_does_not_hide_next():
    queue = QueuedApprovalPrompt()
    first = asyncio.create_task(queue(request()))
    second = asyncio.create_task(queue(replace(request(), request_id="second_request")))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert queue.next_request()["request_id"] == "second_request"
    queue.deny_all()
    assert await second is ApprovalDecision.DENY
    assert queue.pending_count == 0


@pytest.mark.asyncio
async def test_shell_always_is_separate_scoped_persistent_and_revocable(tmp_path):
    database = AgentDatabase(tmp_path / "permissions.sqlite3")
    calls = []

    async def prompt(value):
        calls.append(value)
        assert value.persistent_shell_eligible
        assert not value.persistent_read_eligible
        return ApprovalDecision.ALLOW_ALWAYS_SHELL

    manager = ApprovalManager(prompt, database)
    value = replace(request(), command={"kind": "exec", "executable": "dotnet"})
    scope = ('{"cwd":"c:\\"}', "root-id")
    await manager.request(value, session_eligible=False, shell_scope=scope)
    await ApprovalManager(prompt, database).request(
        value, session_eligible=False, shell_scope=scope
    )
    assert len(calls) == 1
    assert database.list_read_permissions() == []
    for changed in (
        replace(value, grant_id="other"),
        replace(value, project_id="other"),
        replace(value, link_id="other"),
        replace(value, account_id="other"),
    ):
        await manager.request(changed, session_eligible=False, shell_scope=scope)
    assert len(calls) == 5
    await manager.request(value, session_eligible=False, shell_scope=(scope[0], "new-root"))
    assert len(calls) == 6
    assert manager.revoke_shell_permissions() == 5
    await manager.request(value, session_eligible=False, shell_scope=scope)
    assert len(calls) == 7
    with pytest.raises(AgentError):
        await manager.request(
            replace(value, capability="files:write"), session_eligible=False, shell_scope=scope
        )
