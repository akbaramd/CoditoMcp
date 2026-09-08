import base64
import hashlib
from datetime import UTC, datetime

import pytest
from codito_protocol.screenshot import (
    DeviceDisplaysResult,
    DeviceScreenshotInput,
    DeviceScreenshotResult,
    durable_tool_result,
)
from pydantic import ValidationError

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA9hAAAPYQGoP6dp"
    "AAAADElEQVQImWNgYGAAAAAEAAGjChXjAAAAAElFTkSuQmCC"
)


def screenshot_value():
    return {
        "width": 1,
        "height": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
        "image_base64": PNG,
    }


def test_valid_image_and_private_journals():
    value = DeviceScreenshotResult.model_validate(screenshot_value()).model_dump(mode="json")
    original = {"ok": True, "result": value}
    stored = durable_tool_result(original)
    assert "image_base64" not in str(stored)
    assert stored["error"]["retryable"]
    assert original["result"]["image_base64"] == PNG
    assert durable_tool_result(stored) == stored


@pytest.mark.parametrize(
    "field,value",
    [
        ("width", 2),
        ("height", 3000),
        ("sha256", "0" * 64),
        ("image_base64", "not base64!"),
        ("captured_at", "2026-09-08T00:00:00"),
        ("image_base64", base64.b64encode(b"not a png").decode()),
    ],
)
def test_screenshot_result_rejects_bad_data(field, value):
    with pytest.raises(ValidationError):
        DeviceScreenshotResult.model_validate({**screenshot_value(), field: value})


@pytest.mark.parametrize(
    "extra", [{"approved": True}, {"display": "all"}, {"max_dimension": 10000}]
)
def test_capture_input_never_accepts_approval_or_unbounded_size(extra):
    with pytest.raises(ValidationError):
        DeviceScreenshotInput.model_validate({"purpose": "Review UI", **extra})


def test_selected_display_and_catalog_contract():
    identifier = "screen_" + "a" * 64
    request = DeviceScreenshotInput(purpose="Choose monitor", action="list_displays")
    assert request.display == "primary"
    assert DeviceScreenshotInput(purpose="Monitor", display=identifier).display == identifier
    catalog = {
        "displays": [
            {
                "id": identifier,
                "label": "Monitor",
                "primary": True,
                "width": 1920,
                "height": 1080,
                "scale_factor": 1,
                "identity": "a" * 64,
                "persistent_permission_supported": True,
            }
        ],
        "topology_id": "b" * 64,
    }
    result = DeviceDisplaysResult.model_validate(catalog)
    assert result.action == "list_displays"
    assert "image_base64" not in result.model_dump()
    with pytest.raises(ValidationError, match="unique"):
        DeviceDisplaysResult.model_validate({**catalog, "displays": catalog["displays"] * 2})
    with pytest.raises(ValidationError, match="primary"):
        DeviceDisplaysResult.model_validate(
            {**catalog, "displays": [{**catalog["displays"][0], "primary": False}]}
        )
