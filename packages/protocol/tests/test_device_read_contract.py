import pytest
from codito_protocol import TOOL_CONTRACTS, DeviceReadInput, DeviceReadResult
from pydantic import ValidationError


@pytest.mark.parametrize(
    "scope",
    [
        "C:",
        "C:relative",
        "//host/share",
        "//?/C:/",
        "C:/..",
        "C:/test/../other",
        "C:/NUL",
        "C:/COM¹",
        "C:/folder.",
        "C:/folder ",
        "C:/a:stream",
        "C:/a//b",
        "C:/.",
        "C:/a\x00",
        "C:/*",
    ],
)
def test_reject_invalid_read_scopes(scope):
    with pytest.raises(ValidationError):
        DeviceReadInput(operation="list_directory", scope_path=scope, purpose="Inspect")


@pytest.mark.parametrize(
    "extra",
    [
        {"approved": True},
        {"trusted": True},
        {"allow_always": True},
        {"project_id": "C:/not-an-opaque-project"},
    ],
)
def test_model_cannot_supply_read_authority(extra):
    with pytest.raises(ValidationError):
        DeviceReadInput.model_validate(
            {"operation": "list_directory", "scope_path": "C:/", "purpose": "Inspect", **extra}
        )


def test_drive_scope_and_relative_path_are_separate():
    request = DeviceReadInput(
        operation="read_file", scope_path="c:\\", path="example/file.txt", purpose="Inspect"
    )
    assert request.scope_path == "C:/"
    with pytest.raises(ValidationError):
        DeviceReadInput(
            operation="read_file", scope_path="C:/", path="../outside", purpose="Inspect"
        )
    assert TOOL_CONTRACTS["device_read"]["annotations"]["readOnlyHint"]
    assert DeviceReadResult(operation="list_directory", scope_path="C:/", path="").entries == []
