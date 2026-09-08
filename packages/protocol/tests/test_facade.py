from __future__ import annotations

import pytest
from codito_protocol import (
    validate_project_apply_patch,
    validate_project_manage,
    validate_project_read,
    validate_project_shell,
)
from codito_protocol.desktop_action import DeviceDesktopInput
from codito_protocol.facade import FACADE_MODELS, facade_wire_request
from codito_protocol.facade_contracts import FACADE_CONTRACTS
from codito_protocol.screenshot import DeviceScreenshotInput
from pydantic import ValidationError

PROJECT = "project_abcdefghijkl"
KEY = "idempotency_abcdefghijkl"
JOB = "job_abcdefghijklmnop"
HASH = "a" * 64
TARGET = {"project_id": PROJECT, "purpose": "User-requested test"}
PATCH = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch\n"
CASES = [
    ("projects_list", {}, "project_read", "list_projects"),
    (
        "project_add",
        {"title": "Demo", "idempotency_key": KEY},
        "project_manage",
        "request_add_project",
    ),
    (
        "project_rename",
        {"project_id": PROJECT, "title": "Renamed", "idempotency_key": KEY},
        "project_manage",
        "rename_project",
    ),
    (
        "project_remove",
        {"project_id": PROJECT, "idempotency_key": KEY},
        "project_manage",
        "remove_project",
    ),
    ("directory_list", TARGET, "project_read", "list_directory"),
    ("file_read", {**TARGET, "path": "a.txt"}, "project_read", "read_file"),
    ("text_search", {**TARGET, "query": "needle"}, "project_read", "search_text"),
    (
        "file_patch",
        {**TARGET, "patch": PATCH, "base_hashes": {"a.txt": HASH}, "idempotency_key": KEY},
        "project_apply_patch",
        None,
    ),
    (
        "file_delete",
        {**TARGET, "path": "a.txt", "base_hash": HASH, "idempotency_key": KEY},
        "project_apply_patch",
        None,
    ),
    (
        "execute_shell",
        {**TARGET, "command": "dotnet --info", "idempotency_key": KEY},
        "project_shell",
        "start",
    ),
    ("shell_status", {"project_id": PROJECT, "job_id": JOB}, "project_shell", "poll"),
    ("shell_cancel", {"project_id": PROJECT, "job_id": JOB}, "project_shell", "cancel"),
    ("screen_list", {}, "device_screenshot", "list_displays"),
    ("screenshot_capture", TARGET, "device_screenshot", "capture"),
    ("browser_open", {**TARGET, "url": "https://example.com/"}, "device_desktop", "open_browser"),
]


@pytest.mark.parametrize(("name", "arguments", "wire_name", "operation"), CASES)
def test_facade_maps_to_valid_existing_wire(name, arguments, wire_name, operation):
    actual, payload = facade_wire_request(name, arguments)
    assert actual == wire_name
    assert payload.get("operation", payload.get("action")) == operation
    validators = {
        "project_read": validate_project_read,
        "project_manage": validate_project_manage,
        "project_apply_patch": validate_project_apply_patch,
        "project_shell": validate_project_shell,
        "device_screenshot": DeviceScreenshotInput.model_validate,
        "device_desktop": DeviceDesktopInput.model_validate,
    }
    validators[wire_name](payload)
    properties = FACADE_MODELS[name].model_json_schema()["properties"]
    assert not {"action", "operation", "approved", "trusted"}.intersection(properties)
    assert FACADE_MODELS[name].model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(("name", "arguments", "wire_name", "operation"), CASES)
@pytest.mark.parametrize("forbidden", ["approved", "trusted", "action", "unknown_field"])
def test_facade_rejects_unknown_or_authority_inputs(
    name, arguments, wire_name, operation, forbidden
):
    with pytest.raises(ValidationError):
        facade_wire_request(name, {**arguments, forbidden: True})


def test_descriptors_have_precise_scopes_static_status_and_titles():
    assert list(FACADE_MODELS) == list(FACADE_CONTRACTS)
    assert len(FACADE_MODELS) == 15
    for contract in FACADE_CONTRACTS.values():
        assert contract["title"]
        for field in ("invoking", "invoked"):
            assert 1 <= len(contract[field]) <= 64
            assert "{" not in contract[field]
        assert contract["securitySchemes"][0]["type"] == "oauth2"
    assert FACADE_CONTRACTS["shell_status"]["annotations"]["readOnlyHint"] is True
    assert FACADE_CONTRACTS["execute_shell"]["annotations"]["destructiveHint"] is True


def test_shell_has_direct_string_command_and_requested_windows_cwd():
    _, payload = facade_wire_request(
        "execute_shell",
        {
            **TARGET,
            "command": "dir",
            "executor": "cmd",
            "cwd": "c:\\Temp",
            "timeout_seconds": 17,
            "idempotency_key": KEY,
            "requested_external_paths": ["c:\\Users"],
        },
    )
    assert payload["command"] == {"kind": "script", "shell": "cmd", "script": "dir"}
    assert payload["external_working_directory"] == "C:/Temp"
    assert payload["requested_external_paths"] == ["C:/Users"]
    assert payload["timeout_seconds"] == 17
    assert "execution" not in payload
    _, inside = facade_wire_request(
        "execute_shell",
        {
            **TARGET,
            "command": "uv --version",
            "cwd": "src\\app",
            "idempotency_key": KEY,
        },
    )
    assert inside["working_directory"] == "src/app"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", {"kind": "script"}),
        ("command", "bad\x00value"),
        ("command", "x" * (256 * 1024 + 1)),
        ("executor", "bash"),
        ("cwd", "C:relative"),
        ("cwd", "../outside"),
        ("cwd", "\\\\server\\share"),
        ("timeout_seconds", 0),
        ("timeout_seconds", 1801),
    ],
    ids=[
        "object-command",
        "nul-command",
        "oversize-command",
        "unknown-executor",
        "drive-relative",
        "traversal",
        "unc",
        "zero-timeout",
        "large-timeout",
    ],
)
def test_invalid_shell_inputs_fail_before_wire(field, value):
    with pytest.raises(ValidationError):
        facade_wire_request(
            "execute_shell",
            {
                **TARGET,
                "command": "dir",
                "idempotency_key": KEY,
                field: value,
            },
        )


@pytest.mark.parametrize(
    "name", ["directory_list", "file_read", "text_search", "file_patch", "file_delete"]
)
def test_external_file_scope_preserves_project_and_uses_existing_wire(name):
    arguments = next(dict(args) for candidate, args, _, _ in CASES if candidate == name)
    wire, payload = facade_wire_request(name, {**arguments, "scope_path": "c:\\Temp"})
    assert wire in {"project_read", "project_apply_patch"}
    assert payload["project_id"] == PROJECT
    assert payload["scope_path"] == "C:/Temp"
    assert payload["purpose"] == TARGET["purpose"]


def test_delete_is_exact_hash_guarded_single_file_patch():
    _, payload = facade_wire_request(
        "file_delete",
        {
            **TARGET,
            "path": "src/a.py",
            "base_hash": HASH,
            "idempotency_key": KEY,
            "dry_run": True,
        },
    )
    assert payload["patch"] == "*** Begin Patch\n*** Delete File: src/a.py\n*** End Patch\n"
    assert payload["base_hashes"] == {"src/a.py": HASH}
    assert payload["dry_run"] is True
    for path in ("", "../a", "a\n*** Delete File: b"):
        with pytest.raises(ValidationError):
            facade_wire_request(
                "file_delete", {**TARGET, "path": path, "base_hash": HASH, "idempotency_key": KEY}
            )


def test_read_range_patch_preconditions_and_url_validation_remain_strict():
    _, read = facade_wire_request(
        "file_read", {**TARGET, "path": "a.txt", "start_line": 6, "max_lines": 7}
    )
    assert read["end_line"] == 12
    assert read["max_lines"] == 7
    legacy = validate_project_read(
        {"operation": "read_file", "project_id": PROJECT, "path": "a.txt"}
    )
    assert "max_lines" not in legacy.model_dump(exclude_none=True)
    with pytest.raises(ValidationError):
        facade_wire_request(
            "shell_cancel", {"project_id": PROJECT, "job_id": JOB, "reason": "x" * 501}
        )
    with pytest.raises(ValidationError):
        facade_wire_request("file_read", {**TARGET, "path": ""})
    with pytest.raises(ValidationError):
        facade_wire_request(
            "file_patch",
            {**TARGET, "patch": PATCH, "base_hashes": {"b.txt": HASH}, "idempotency_key": KEY},
        )
    with pytest.raises(ValidationError):
        facade_wire_request("browser_open", {**TARGET, "url": "file:///C:/secret.txt"})
    for validator, data in (
        (validate_project_read, {"operation": "read_file", "project_id": PROJECT, "path": "a.txt"}),
        (
            validate_project_apply_patch,
            {
                "project_id": PROJECT,
                "patch": PATCH,
                "base_hashes": {"a.txt": HASH},
                "idempotency_key": KEY,
            },
        ),
    ):
        with pytest.raises(ValidationError):
            validator({**data, "scope_path": "C:/Temp"})
