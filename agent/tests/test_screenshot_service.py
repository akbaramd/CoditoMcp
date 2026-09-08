import asyncio
import base64
import copy
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from codito_protocol.screenshot import DeviceScreenshotInput

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode
from codito_agent.screen_capture import DeviceScreenshotService, ScreenCaptureQueue

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA9hAAAPYQGoP6dp"
    "AAAADElEQVQImWNgYGAAAAAEAAGjChXjAAAAAElFTkSuQmCC"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,prompt_count",
    [
        (ProjectMode.NATIVE_APPROVAL, 2),
        (ProjectMode.NATIVE_PROJECT, 1),
        (ProjectMode.FULL_ACCESS, 0),
        (ProjectMode.NATIVE_TRUSTED, 2),
    ],
)
async def test_project_policy_controls_screen_consent_without_upgrading_legacy(
    tmp_path, project_root, mode, prompt_count
):
    database = AgentDatabase(tmp_path / "project-screens.sqlite")
    project = database.register_project("Screens", project_root, mode)
    prompts = []

    async def prompt(request):
        prompts.append(request)
        return (
            ApprovalDecision.ALLOW_ALWAYS_SCREEN
            if mode is ProjectMode.NATIVE_PROJECT
            else ApprovalDecision.ALLOW_ONCE
        )

    capture = ScreenCaptureQueue()
    service = DeviceScreenshotService(
        ApprovalManager(prompt, database),
        capture,
        account_id="account",
        device_id="device",
        database=database,
    )
    request = DeviceScreenshotInput(
        purpose="Synthetic screen policy", project_id=project.project_id
    )
    for _ in range(2):
        result = await execute(service, capture, request=request)
        assert result.structured["image_base64"] == PNG
    assert len(prompts) == prompt_count


SCREEN_A = "screen_" + "a" * 64
SCREEN_B = "screen_" + "b" * 64
CATALOG = {
    "displays": [
        {
            "id": identifier,
            "label": f"Display {index + 1}",
            "primary": index == 0,
            "width": 1920,
            "height": 1080,
            "scale_factor": 1,
            "identity": identifier.removeprefix("screen_"),
            "persistent_permission_supported": True,
        }
        for index, identifier in enumerate((SCREEN_A, SCREEN_B))
    ],
    "topology_id": "c" * 64,
}


async def execute(service, capture, *, catalog=None, responses=None, **changes):
    request = changes.pop("request", DeviceScreenshotInput(purpose="Synthetic UI test"))
    task = asyncio.create_task(
        service.execute(
            request,
            **{
                "grant_id": "grant",
                "link_id": "link",
                "connection_epoch": 1,
                "deadline_at": datetime.now(UTC) + timedelta(seconds=3),
                **changes,
            },
        )
    )
    while not task.done():
        pending = capture.next_request()
        if pending is None:
            await asyncio.sleep(0.001)
            continue
        # Each request is claimable once, even if GUI/IPC crashes afterwards.
        assert capture.next_request() is None
        if responses is not None:
            responses.append(pending)
        if pending["action"] == "list_displays":
            response = copy.deepcopy(catalog or CATALOG)
        else:
            response = {
                "display": pending["display_id"],
                "width": 1,
                "height": 1,
                "captured_at": datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
                "image_base64": PNG,
            }
        assert not capture.respond("another_capture", response)
        assert capture.respond(pending["capture_id"], response)
        assert not capture.respond(pending["capture_id"], response)
    return await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        ApprovalDecision.DENY,
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.ALLOW_ALWAYS_READ,
        ApprovalDecision.ALLOW_ALWAYS_SHELL,
        ApprovalDecision.ALLOW_ALWAYS_SCREEN,
    ],
)
async def test_capture_requires_separate_screen_decision(decision):
    capture = ScreenCaptureQueue()

    async def prompt(value):
        assert capture.next_request() is None  # Metadata only before consent; no pixels.
        assert value.capability == "screen:read"
        assert not value.persistent_read_eligible and not value.persistent_shell_eligible
        assert not value.persistent_screen_eligible  # No permission database configured.
        return decision

    approvals = ApprovalManager(prompt)
    service = DeviceScreenshotService(approvals, capture, account_id="account", device_id="device")
    responses = []
    if decision is not ApprovalDecision.ALLOW_ONCE:
        with pytest.raises(AgentError):
            await execute(service, capture, responses=responses)
        assert [item["action"] for item in responses] == ["list_displays"]
    else:
        result = await execute(service, capture, responses=responses)
        assert result.structured["image_base64"] == PNG
        assert responses[-1]["display_id"] == SCREEN_A
        assert responses[-1]["topology_id"] == CATALOG["topology_id"]


@pytest.mark.asyncio
async def test_catalog_never_requests_pixels_or_approval():
    async def prompt(_request):
        pytest.fail("Metadata listing must not request image consent")

    capture = ScreenCaptureQueue()
    service = DeviceScreenshotService(
        ApprovalManager(prompt), capture, account_id="a", device_id="d"
    )
    responses = []
    result = await execute(
        service,
        capture,
        request=DeviceScreenshotInput(action="list_displays", purpose="Choose display"),
        responses=responses,
    )
    assert result.structured["action"] == "list_displays"
    assert len(result.structured["displays"]) == 2
    assert [item["action"] for item in responses] == ["list_displays"]
    assert "image_base64" not in str(result.structured)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["account", "device", "grant", "link", "display", "topology"])
async def test_always_screen_is_separate_selected_and_identity_bound(tmp_path, binding):
    database = AgentDatabase(tmp_path / "screens.sqlite")
    decisions = []

    async def prompt(request):
        decisions.append(request)
        assert request.persistent_screen_eligible
        return (
            ApprovalDecision.ALLOW_ALWAYS_SCREEN if len(decisions) == 1 else ApprovalDecision.DENY
        )

    capture = ScreenCaptureQueue()
    approvals = ApprovalManager(prompt, database)
    service = DeviceScreenshotService(approvals, capture, account_id="account", device_id="device")
    await execute(service, capture)
    await execute(service, capture, connection_epoch=2)
    assert len(decisions) == 1
    assert len(database.list_screen_permissions()) == 1
    assert database.list_read_permissions() == database.list_shell_permissions() == []
    changes = {}
    if binding in ("account", "device"):
        service = DeviceScreenshotService(
            approvals,
            capture,
            account_id="other" if binding == "account" else "account",
            device_id="other" if binding == "device" else "device",
        )
    elif binding in ("grant", "link"):
        changes[binding + "_id"] = "other"
    elif binding == "display":
        changes["request"] = DeviceScreenshotInput(purpose="Other display", display=SCREEN_B)
    else:
        changed = copy.deepcopy(CATALOG)
        changed["topology_id"] = "d" * 64
        changes["catalog"] = changed
    with pytest.raises(AgentError, match="denied"):
        await execute(service, capture, **changes)
    assert len(decisions) == 2
    assert approvals.revoke_screen_permissions() == 1
    assert database.list_screen_permissions() == []


@pytest.mark.asyncio
async def test_primary_reselection_requires_consent_and_serialless_display_is_one_shot(tmp_path):
    decisions = []

    async def prompt(request):
        decisions.append(request)
        return ApprovalDecision.ALLOW_ALWAYS_SCREEN

    capture = ScreenCaptureQueue()
    approvals = ApprovalManager(prompt, AgentDatabase(tmp_path / "screens.sqlite"))
    service = DeviceScreenshotService(approvals, capture, account_id="account", device_id="device")
    await execute(service, capture)
    catalog = copy.deepcopy(CATALOG)
    for display in catalog["displays"]:
        display["primary"] = not display["primary"]
        display["persistent_permission_supported"] = False
    catalog["topology_id"] = "d" * 64
    with pytest.raises(AgentError, match="Always allow screen is unavailable"):
        await execute(service, capture, catalog=catalog)
    assert decisions[0].project_id == SCREEN_A
    assert decisions[1].project_id == SCREEN_B
    assert not decisions[1].persistent_screen_eligible


@pytest.mark.asyncio
async def test_revocation_or_disconnect_during_consent_never_requests_pixels(tmp_path):
    async def prompt(_request):
        approvals.revoke_screen_permissions()
        return ApprovalDecision.ALLOW_ALWAYS_SCREEN

    capture = ScreenCaptureQueue()
    approvals = ApprovalManager(prompt, AgentDatabase(tmp_path / "screens.sqlite"))
    service = DeviceScreenshotService(approvals, capture, account_id="account", device_id="device")
    responses = []
    with pytest.raises(AgentError, match="invalidated"):
        await execute(service, capture, responses=responses)
    assert [item["action"] for item in responses] == ["list_displays"]
    assert approvals.revoke_screen_permissions() == 0


@pytest.mark.asyncio
async def test_unknown_screen_never_prompts_or_captures():
    async def prompt(_request):
        pytest.fail("Unknown screen cannot request consent")

    capture = ScreenCaptureQueue()
    service = DeviceScreenshotService(
        ApprovalManager(prompt), capture, account_id="a", device_id="d"
    )
    with pytest.raises(AgentError, match="Selected display is unavailable"):
        await execute(
            service,
            capture,
            request=DeviceScreenshotInput(purpose="Missing display", display="screen_" + "e" * 64),
        )


@pytest.mark.asyncio
async def test_claimed_capture_timeout_cannot_be_replayed():
    capture = ScreenCaptureQueue()
    deadline = datetime.now(UTC) + timedelta(milliseconds=25)
    task = asyncio.create_task(
        capture.capture(1600, deadline, display_id=SCREEN_A, topology_id="a" * 64)
    )
    await asyncio.sleep(0)
    pending = capture.next_request()
    assert pending
    assert capture.next_request() is None
    with pytest.raises(AgentError, match="before the deadline"):
        await task
    assert not capture.respond(pending["capture_id"], {"ok": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [False, True])
async def test_cancelled_capture_leaves_no_pending_request(claimed):
    capture = ScreenCaptureQueue()
    task = asyncio.create_task(
        capture.capture(
            1600,
            datetime.now(UTC) + timedelta(seconds=3),
            display_id=SCREEN_A,
            topology_id="a" * 64,
        )
    )
    await asyncio.sleep(0)
    pending = capture.next_request() if claimed else None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert capture.next_request() is None
    assert capture._pending == {}
    if pending:
        assert not capture.respond(pending["capture_id"], {"ok": True})


@pytest.mark.asyncio
async def test_revoked_queued_capture_is_never_delivered_to_tray():
    def expired():
        raise AgentError("approval_expired", "Permission revoked")

    capture = ScreenCaptureQueue()
    task = asyncio.create_task(
        capture.capture(
            1600,
            datetime.now(UTC) + timedelta(seconds=3),
            display_id=SCREEN_A,
            topology_id="a" * 64,
            ensure_current=expired,
        )
    )
    await asyncio.sleep(0)
    assert capture.next_request() is None
    with pytest.raises(AgentError, match="Permission revoked"):
        await task
    assert capture._pending == {}
