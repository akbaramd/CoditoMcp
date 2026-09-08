import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from codito_protocol.desktop_action import DeviceDesktopInput

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.db import AgentDatabase
from codito_agent.desktop_actions import DesktopActionQueue, DeviceDesktopService
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,allowed",
    [
        (ProjectMode.FULL_ACCESS, True),
        (ProjectMode.NATIVE_PROJECT, False),
        (ProjectMode.NATIVE_APPROVAL, False),
        (ProjectMode.NATIVE_TRUSTED, False),
    ],
)
async def test_browser_full_access_is_explicit_not_legacy_trust(
    tmp_path, project_root, mode, allowed
):
    database = AgentDatabase(tmp_path / "browser-policy.sqlite")
    project = database.register_project("Browser", project_root, mode)
    prompts = []

    async def prompt(request):
        prompts.append(request)
        return ApprovalDecision.DENY

    queue = DesktopActionQueue()
    service = DeviceDesktopService(
        ApprovalManager(prompt, database),
        queue,
        account_id="account",
        device_id="device",
        database=database,
    )
    task = asyncio.create_task(
        service.execute(
            DeviceDesktopInput(
                url="https://example.com/", purpose="Synthetic", project_id=project.project_id
            ),
            grant_id="grant",
            link_id="link",
            connection_epoch=1,
            deadline_at=datetime.now(UTC) + timedelta(seconds=3),
        )
    )
    if allowed:
        pending = await pending_request(queue)
        queue.respond(pending["desktop_action_id"], {"ok": True})
        assert (await task).structured["status"] == "submitted"
        assert not prompts
    else:
        with pytest.raises(AgentError, match="denied"):
            await task
        assert len(prompts) == 1 and queue.next_request() is None


async def pending_request(queue):
    for _ in range(100):
        value = queue.next_request()
        if value is not None:
            return value
        await asyncio.sleep(0.001)
    pytest.fail("Desktop action was not queued")


def execute(service, *, seconds=3):
    return asyncio.create_task(
        service.execute(
            DeviceDesktopInput(url="https://example.com/", purpose="Synthetic browser test"),
            grant_id="grant",
            link_id="link",
            connection_epoch=5,
            deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
        )
    )


@pytest.mark.parametrize("decision", list(ApprovalDecision))
async def test_browser_always_requires_its_own_one_shot_approval(decision):
    queue = DesktopActionQueue()

    async def prompt(value):
        assert queue.next_request() is None
        assert value.capability == "desktop:open_url"
        assert value.account_id == "account" and value.device_id == "device"
        assert value.grant_id == "grant" and value.link_id == "link"
        assert value.connection_epoch == 5
        assert not value.session_eligible
        assert not value.persistent_read_eligible and not value.persistent_shell_eligible
        assert not getattr(value, "persistent_screen_eligible", False)
        assert value.requested_network
        assert "https://example.com/" in value.summary
        assert "browser profile" in value.summary
        return decision

    service = DeviceDesktopService(
        ApprovalManager(prompt), queue, account_id="account", device_id="device"
    )
    task = execute(service)
    if decision is not ApprovalDecision.ALLOW_ONCE:
        with pytest.raises(AgentError):
            await task
        assert queue.next_request() is None
        return
    pending = await pending_request(queue)
    assert queue.next_request() is None  # Polling cannot launch the same URL twice.
    assert not queue.respond("unrelated", {"ok": True})
    assert queue.respond(pending["desktop_action_id"], {"ok": True})
    assert not queue.respond(pending["desktop_action_id"], {"ok": True})
    result = await task
    assert result.structured == {
        "action": "open_browser",
        "browser": "default",
        "url": "https://example.com/",
        "status": "submitted",
    }
    assert "does not confirm" in result.text


async def test_firefox_selection_is_bound_to_the_approval_and_queue():
    queue = DesktopActionQueue()

    async def prompt(value):
        assert "firefox" in value.project_title
        assert value.command["browser"] == "firefox"
        assert "OPEN BROWSER (firefox)" in value.summary
        return ApprovalDecision.ALLOW_ONCE

    service = DeviceDesktopService(
        ApprovalManager(prompt), queue, account_id="account", device_id="device"
    )
    task = asyncio.create_task(
        service.execute(
            DeviceDesktopInput(browser="firefox", url="https://example.com/", purpose="Test"),
            grant_id="grant",
            link_id="link",
            connection_epoch=1,
            deadline_at=datetime.now(UTC) + timedelta(seconds=3),
        )
    )
    pending = await pending_request(queue)
    assert pending["browser"] == "firefox"
    queue.respond(pending["desktop_action_id"], {"ok": True})
    assert (await task).structured["browser"] == "firefox"


@pytest.mark.parametrize("claimed", [False, True])
async def test_desktop_timeout_is_honest_and_not_replayed(claimed):
    queue = DesktopActionQueue()

    async def prompt(value):
        return ApprovalDecision.ALLOW_ONCE

    service = DeviceDesktopService(
        ApprovalManager(prompt), queue, account_id="account", device_id="device"
    )
    task = execute(service, seconds=0.05)
    if claimed:
        pending = await pending_request(queue)
    with pytest.raises(AgentError) as exc:
        await task
    assert exc.value.code == ("outcome_unknown" if claimed else "desktop_unavailable")
    assert queue.next_request() is None
    if claimed:
        assert not queue.respond(pending["desktop_action_id"], {"ok": True})


async def test_os_refusal_is_not_reported_as_success():
    queue = DesktopActionQueue()

    async def prompt(value):
        return ApprovalDecision.ALLOW_ONCE

    service = DeviceDesktopService(
        ApprovalManager(prompt), queue, account_id="account", device_id="device"
    )
    task = execute(service)
    pending = await pending_request(queue)
    queue.respond(pending["desktop_action_id"], {"ok": False})
    with pytest.raises(AgentError, match="desktop_unavailable"):
        await task


@pytest.mark.parametrize("after_claim", [False, True])
async def test_connection_change_invalidates_pending_browser_action(after_claim):
    queue = DesktopActionQueue()
    approved = asyncio.Event()

    async def prompt(value):
        approved.set()
        return ApprovalDecision.ALLOW_ONCE

    approvals = ApprovalManager(prompt)
    service = DeviceDesktopService(approvals, queue, account_id="account", device_id="device")
    task = execute(service)
    if after_claim:
        pending = await pending_request(queue)
        approvals.invalidate_pending("disconnect")
        assert queue.respond(pending["desktop_action_id"], {"ok": True})
    else:
        await approved.wait()
        await asyncio.sleep(0.005)
        approvals.invalidate_pending("disconnect")
        assert queue.next_request() is None
    with pytest.raises(AgentError, match="approval_expired"):
        await task


async def test_cancellation_removes_pending_action():
    queue = DesktopActionQueue()

    async def prompt(value):
        return ApprovalDecision.ALLOW_ONCE

    service = DeviceDesktopService(
        ApprovalManager(prompt), queue, account_id="account", device_id="device"
    )
    task = execute(service)
    pending = await pending_request(queue)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.next_request() is None
    assert not queue.respond(pending["desktop_action_id"], {"ok": True})


async def test_approval_expiry_never_dispatches_url():
    queue = DesktopActionQueue()

    async def prompt(value):
        await asyncio.sleep(1)
        return ApprovalDecision.ALLOW_ONCE

    service = DeviceDesktopService(
        ApprovalManager(prompt), queue, account_id="account", device_id="device"
    )
    with pytest.raises(AgentError, match="approval_expired"):
        await execute(service, seconds=0.02)
    assert queue.next_request() is None
