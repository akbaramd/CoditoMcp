import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from codito_protocol.screenshot import DeviceScreenshotInput

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.errors import AgentError
from codito_agent.screen_capture import DeviceScreenshotService, ScreenCaptureQueue

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA9hAAAPYQGoP6dp"
    "AAAADElEQVQImWNgYGAAAAAEAAGjChXjAAAAAElFTkSuQmCC"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        ApprovalDecision.DENY,
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.ALLOW_ALWAYS_READ,
        ApprovalDecision.ALLOW_ALWAYS_SHELL,
    ],
)
async def test_capture_requires_separate_screen_decision(decision):
    capture = ScreenCaptureQueue()

    async def prompt(value):
        assert capture.next_request() is None  # No pixels requested before consent.
        assert value.capability == "screen:read"
        assert not value.persistent_read_eligible and not value.persistent_shell_eligible
        return decision

    approvals = ApprovalManager(prompt)
    service = DeviceScreenshotService(approvals, capture, account_id="account", device_id="device")
    task = asyncio.create_task(
        service.execute(
            DeviceScreenshotInput(purpose="Synthetic UI test"),
            grant_id="grant",
            link_id="link",
            connection_epoch=1,
            deadline_at=datetime.now(UTC) + timedelta(seconds=3),
        )
    )
    if decision is not ApprovalDecision.ALLOW_ONCE:
        with pytest.raises(AgentError):
            await task
        assert capture.next_request() is None
        return
    for _ in range(30):
        if capture.next_request() is not None:
            break
        await asyncio.sleep(0.01)
    pending = capture.next_request()
    assert pending is not None
    value = {
        "width": 1,
        "height": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
        "image_base64": PNG,
    }
    assert not capture.respond("another_capture", value)
    assert capture.respond(pending["capture_id"], value)
    result = await task
    assert result.structured["image_base64"] == PNG
    assert not capture.respond(pending["capture_id"], value)
