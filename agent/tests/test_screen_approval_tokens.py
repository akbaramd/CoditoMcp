import asyncio
from dataclasses import replace

import pytest
from test_approvals import request

from codito_agent.approvals import ApprovalDecision, ApprovalManager, ApprovalRisk
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.ipc import QueuedApprovalPrompt


@pytest.mark.asyncio
async def test_screen_token_is_bound_and_review_does_not_approve():
    queue = QueuedApprovalPrompt()
    task = asyncio.create_task(queue(replace(request(), persistent_screen_eligible=True)))
    await asyncio.sleep(0)
    pending = queue.next_request()
    identifier = pending["request_id"]
    tokens = pending["toast_tokens"]
    assert queue.consume_review_request() is None
    assert not queue.respond_toast(identifier, "review", tokens["allow_once"])
    assert queue.respond_toast(identifier, "review", tokens["review"])
    assert queue.consume_review_request() == identifier
    assert queue.consume_review_request() is None
    assert queue.is_pending(identifier) and not task.done()
    assert not queue.respond_toast(identifier, "allow_always_shell", tokens["allow_always_screen"])
    assert queue.respond_toast(identifier, "allow_always_screen", tokens["allow_always_screen"])
    assert not queue.respond_toast(identifier, "allow_always_screen", tokens["allow_always_screen"])
    assert await task is ApprovalDecision.ALLOW_ALWAYS_SCREEN
    assert queue.consume_review_request() is None


@pytest.mark.asyncio
async def test_review_resolves_requested_pending_item_not_first():
    queue = QueuedApprovalPrompt()
    first = asyncio.create_task(queue(replace(request(), request_id="first")))
    second = asyncio.create_task(queue(replace(request(), request_id="second")))
    await asyncio.sleep(0)
    pending = queue.next_request("second")
    assert pending["request_id"] == "second"
    assert queue.next_request()["request_id"] == "first"
    assert queue.respond_toast("second", "review", pending["toast_tokens"]["review"])
    assert queue.consume_review_request() == "second"
    assert queue.next_request("missing") is None
    queue.deny_all()
    assert await first is await second is ApprovalDecision.DENY
    assert not queue.respond_toast("second", "review", pending["toast_tokens"]["review"])
    assert queue.consume_review_request() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["read", "shell"])
async def test_screen_decision_cannot_authorize_another_capability(tmp_path, scope):
    async def prompt(_value):
        return ApprovalDecision.ALLOW_ALWAYS_SCREEN

    manager = ApprovalManager(prompt, AgentDatabase(tmp_path / "permissions.sqlite"))
    value = replace(request(), command={"kind": "exec", "executable": "dotnet"})
    values = {"shell_scope": ("scope", "identity")}
    if scope == "read":
        value = replace(
            value,
            command=None,
            capability="device:read",
            risk=ApprovalRisk.READ,
            requested_external_paths=("C:\\",),
        )
        values = {"read_scope": ("C:\\", "identity")}
    with pytest.raises(AgentError, match="Always allow screen is unavailable"):
        await manager.request(value, session_eligible=False, **values)
