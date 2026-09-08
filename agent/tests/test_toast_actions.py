import asyncio
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
from test_approvals import request

from codito_agent.approvals import ApprovalDecision
from codito_agent.ipc import QueuedApprovalPrompt
from codito_agent.notifications import activation_request, toast_specification


@pytest.mark.asyncio
async def test_toast_buttons_are_bound_one_use_and_xml_escaped():
    queue = QueuedApprovalPrompt()
    task = asyncio.create_task(
        queue(replace(request(), persistent_shell_eligible=True, summary="<malicious & text>"))
    )
    await asyncio.sleep(0)
    pending = queue.next_request()
    pending["deadline_at"] = str(pending["deadline_at"])
    assert "T" in toast_specification(pending)["expires_at"]
    document = ET.fromstring(toast_specification(pending)["xml"])  # noqa: S314 - generated test XML.
    buttons = document.findall("actions/action")
    assert [b.attrib["content"] for b in buttons] == ["Deny", "Allow", "Always allow"]
    assert document.attrib["activationType"] == "foreground"
    assert all(button.attrib["activationType"] == "foreground" for button in buttons)
    clicked = activation_request(buttons[2].attrib["arguments"])
    args = {k: v for k, v in clicked.items() if k != "action"}
    assert not queue.respond_toast(**{**args, "decision": "deny"})
    assert not queue.respond_toast(**{**args, "request_id": "different"})
    assert not queue.respond_toast(**{**args, "token": "wrong"})
    assert queue.respond_toast(**args)
    assert not queue.respond_toast(**args)
    assert await task is ApprovalDecision.ALLOW_ALWAYS_SHELL


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.test/",
        "codito-approval://evil",
        "codito-approval://decision?request_id=x&decision=allow_once&token=x&token=y",
    ],
)
def test_bad_activation_never_reaches_ipc(uri):
    with pytest.raises(ValueError):
        activation_request(uri)


@pytest.mark.asyncio
async def test_screen_always_button_has_separate_one_use_decision():
    queue = QueuedApprovalPrompt()
    task = asyncio.create_task(queue(replace(request(), persistent_screen_eligible=True)))
    await asyncio.sleep(0)
    pending = queue.next_request()
    document = ET.fromstring(toast_specification(pending)["xml"])  # noqa: S314 - generated XML.
    button = document.findall("actions/action")[2]
    assert button.attrib["content"] == "Always allow"
    clicked = activation_request(button.attrib["arguments"])
    assert clicked["decision"] == "allow_always_screen"
    args = {k: v for k, v in clicked.items() if k != "action"}
    assert not queue.respond_toast(**{**args, "decision": "allow_always_shell"})
    assert queue.respond_toast(**args)
    assert not queue.respond_toast(**args)
    assert await task is ApprovalDecision.ALLOW_ALWAYS_SCREEN
