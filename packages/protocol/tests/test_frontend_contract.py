from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

import pytest
from codito_protocol import (
    TOOL_CONTRACTS,
    FrontendActResult,
    FrontendMatchedRule,
    FrontendSessionStartResult,
    FrontendSnapshotMetadata,
    FrontendSnapshotResult,
    FrontendSourceResult,
    OperationPayload,
    validate_project_frontend,
    validate_project_frontend_result,
)
from codito_protocol.facade import FACADE_MODELS, facade_wire_request
from codito_protocol.facade_contracts import FACADE_CONTRACTS, FACADE_OUTPUT_MODELS
from codito_protocol.schemas import schema_for
from codito_protocol.screenshot import durable_tool_result
from jsonschema import Draft202012Validator
from pydantic import ValidationError

PROJECT = "project_abcdefghijkl"
SESSION = "frontend_session_abcdef"
SNAPSHOT = "frontend_snapshot_abcdef"
KEY = "idempotency_abcdefghijkl"

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA9hAAAPYQGoP6dp"
    "AAAADElEQVQImWNgYGAAAAAEAAGjChXjAAAAAElFTkSuQmCC"
)


def png_for(width: int, height: int) -> bytes:
    # The protocol validates the PNG signature/IHDR/digest without decoding pixels.
    value = bytearray(_PNG_1X1)
    value[16:20] = width.to_bytes(4, "big")
    value[20:24] = height.to_bytes(4, "big")
    return bytes(value)


def snapshot_value() -> dict[str, object]:
    raw = png_for(320, 240)
    return {
        "operation": "snapshot",
        "project_id": PROJECT,
        "session_id": SESSION,
        "snapshot_id": SNAPSHOT,
        "url": "http://localhost:5173/demos/dashboard",
        "title": "Dashboard",
        "viewport": {"width": 320, "height": 240},
        "mime_type": "image/png",
        "width": 320,
        "height": 240,
        "captured_at": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "image_base64": base64.b64encode(raw).decode(),
        "elements": [
            {
                "element_id": "e1",
                "tag": "main",
                "role": "main",
                "box": {"x": 0, "y": 0, "width": 320, "height": 240},
                "visible": True,
            },
            {
                "element_id": "e23",
                "parent_id": "e1",
                "tag": "button",
                "role": "button",
                "name": "Add user",
                "box": {"x": 200, "y": 20, "width": 100, "height": 36},
                "visible": True,
            },
        ],
    }


def start_result_value() -> dict[str, object]:
    return {
        "operation": "session_start",
        "project_id": PROJECT,
        "session_id": SESSION,
        "base_url": "http://localhost:5173",
        "url": "http://localhost:5173/demos/dashboard",
        "viewport": {"width": 320, "height": 240},
        "dev_server": {"ownership": "managed", "health": "ready"},
    }


def test_six_public_tools_route_to_one_hidden_wire_contract() -> None:
    cases = {
        "frontend_session_start": {
            "project_id": PROJECT,
            "purpose": "Open dashboard",
            "route": "/demos/dashboard",
            "idempotency_key": KEY,
        },
        "frontend_snapshot": {
            "project_id": PROJECT,
            "session_id": SESSION,
            "purpose": "Review dashboard",
        },
        "frontend_inspect": {
            "project_id": PROJECT,
            "session_id": SESSION,
            "purpose": "Inspect button",
            "snapshot_id": SNAPSHOT,
            "element_id": "e23",
        },
        "frontend_act": {
            "project_id": PROJECT,
            "session_id": SESSION,
            "purpose": "Check hover",
            "snapshot_id": SNAPSHOT,
            "element_id": "e23",
            "action": "hover",
            "idempotency_key": KEY,
        },
        "frontend_source": {
            "project_id": PROJECT,
            "session_id": SESSION,
            "purpose": "Find component",
            "snapshot_id": SNAPSHOT,
            "element_id": "e23",
        },
        "frontend_session_stop": {
            "project_id": PROJECT,
            "session_id": SESSION,
            "purpose": "Review complete",
        },
    }
    for name, arguments in cases.items():
        wire_name, payload = facade_wire_request(name, arguments)
        assert wire_name == "project_frontend"
        assert OperationPayload(tool_name=wire_name, input=payload).tool_name == wire_name
        assert validate_project_frontend(payload).operation == payload["operation"]
        assert name in FACADE_OUTPUT_MODELS


def test_operation_scopes_are_least_privilege() -> None:
    expected = {
        "frontend_session_start": {"projects:read", "frontend:interact", "shell:execute"},
        "frontend_snapshot": {"projects:read", "frontend:read"},
        "frontend_inspect": {"projects:read", "frontend:read"},
        "frontend_act": {"projects:read", "frontend:interact"},
        "frontend_source": {"projects:read", "frontend:read", "files:read"},
        "frontend_session_stop": {"projects:read", "frontend:interact"},
    }
    for name, scopes in expected.items():
        assert set(FACADE_CONTRACTS[name]["securitySchemes"][0]["scopes"]) == scopes
    assert TOOL_CONTRACTS["project_frontend"]["required_scopes_by_operation"] == {
        name.removeprefix("frontend_"): list(FACADE_CONTRACTS[name]["securitySchemes"][0]["scopes"])
        for name in expected
    }


@pytest.mark.parametrize(
    "target",
    [
        {"snapshot_id": SNAPSHOT, "element_id": "e23"},
        {"snapshot_id": SNAPSHOT, "x": 119, "y": 122},
    ],
)
def test_inspect_target_is_bound_to_one_snapshot(target: dict[str, object]) -> None:
    value = validate_project_frontend(
        {
            "operation": "inspect",
            "project_id": PROJECT,
            "session_id": SESSION,
            "purpose": "Inspect target",
            **target,
        }
    )
    assert value.snapshot_id == SNAPSHOT


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"snapshot_id": SNAPSHOT},
        {"snapshot_id": SNAPSHOT, "element_id": "e23", "x": 1, "y": 2},
        {"snapshot_id": SNAPSHOT, "x": 1},
        {"snapshot_id": SNAPSHOT, "element_id": "button.primary"},
    ],
)
def test_inspect_rejects_unbound_ambiguous_or_selector_targets(target: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        validate_project_frontend(
            {
                "operation": "inspect",
                "project_id": PROJECT,
                "session_id": SESSION,
                "purpose": "Inspect target",
                **target,
            }
        )


@pytest.mark.parametrize(
    ("action", "fields"),
    [
        ("click", {}),
        ("hover", {}),
        ("focus", {}),
        ("fill", {"text": "hello"}),
        ("press", {"key": "Enter"}),
        ("scroll", {"delta_y": 400}),
        ("select", {"option": "active"}),
    ],
)
def test_each_bounded_frontend_action_validates(action: str, fields: dict[str, object]) -> None:
    value = validate_project_frontend(
        {
            "operation": "act",
            "project_id": PROJECT,
            "session_id": SESSION,
            "purpose": "Exercise control",
            "snapshot_id": SNAPSHOT,
            "element_id": "e23",
            "action": action,
            "idempotency_key": KEY,
            **fields,
        }
    )
    assert value.action == action


@pytest.mark.parametrize(
    ("action", "fields"),
    [
        ("fill", {}),
        ("fill", {"text": "x", "key": "Enter"}),
        ("press", {"key": "Control+A"}),
        ("scroll", {"delta_x": 0, "delta_y": 0}),
        ("select", {"text": "wrong"}),
        ("click", {"text": "unexpected"}),
    ],
)
def test_action_specific_fields_are_strict(action: str, fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        validate_project_frontend(
            {
                "operation": "act",
                "project_id": PROJECT,
                "session_id": SESSION,
                "purpose": "Exercise control",
                "snapshot_id": SNAPSHOT,
                "element_id": "e23",
                "action": action,
                "idempotency_key": KEY,
                **fields,
            }
        )


def test_public_inputs_have_no_raw_browser_or_sensitive_authority() -> None:
    forbidden = {
        "selector",
        "javascript",
        "script",
        "files",
        "file_path",
        "upload",
        "password",
        "allow_password",
    }
    for name, model in FACADE_MODELS.items():
        if not name.startswith("frontend_"):
            continue
        assert forbidden.isdisjoint(model.model_json_schema()["properties"])
        base = {"project_id": PROJECT, "purpose": "Attempt authority injection"}
        with pytest.raises(ValidationError):
            model.model_validate({**base, "selector": "button"})


@pytest.mark.parametrize(
    "route",
    [
        "https://example.com/",
        "//example.com/x",
        "dashboard",
        "/bad\\x",
        "/dashboard?token=secret",
        "/dashboard#private",
        "/dashboard\n",
    ],
)
def test_session_start_accepts_only_project_route(route: str) -> None:
    with pytest.raises(ValidationError):
        validate_project_frontend(
            {
                "operation": "session_start",
                "project_id": PROJECT,
                "purpose": "Open route",
                "route": route,
                "idempotency_key": KEY,
            }
        )


@pytest.mark.parametrize(
    "schema_name",
    ["facade-frontend-session-start-input", "project-frontend-input"],
)
def test_exported_start_schemas_reject_unsafe_routes(schema_name: str) -> None:
    validator = Draft202012Validator(schema_for(schema_name))
    base: dict[str, object] = {
        "project_id": PROJECT,
        "purpose": "Open route",
        "route": "/dashboard",
        "idempotency_key": KEY,
    }
    if schema_name == "project-frontend-input":
        base["operation"] = "session_start"
    assert not list(validator.iter_errors(base))
    for route in (
        "https://example.com/",
        "//example.com/x",
        "/dashboard?token=secret",
        "/dashboard#private",
        "/bad\\path",
        "/bad\x7fpath",
        "/dashboard\n",
        "/" + "x" * 2_048,
    ):
        assert list(validator.iter_errors({**base, "route": route})), route


@pytest.mark.parametrize(
    ("base_url", "url"),
    [
        ("http://localhost:5173", "http://localhost:5173/app"),
        ("http://LOCALHOST.:80", "http://LOCALHOST.:80/"),
        ("http://127.12.34.56:5173", "http://127.12.34.56:5173/app"),
        ("http://[::1]:5173", "http://[::1]:5173/app"),
        (
            "http://[0:0:0:0:0:0:0:1]:5173",
            "http://[0:0:0:0:0:0:0:1]:5173/app",
        ),
    ],
)
def test_managed_url_runtime_and_output_schema_accept_loopback(base_url: str, url: str) -> None:
    result = {**start_result_value(), "base_url": base_url, "url": url}
    FrontendSessionStartResult.model_validate(result)
    facade = {"ok": True, "text": "ready", "result": result}
    validator = Draft202012Validator(schema_for("facade-frontend-session-start-output"))
    assert not list(validator.iter_errors(facade))


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:5173/app",
        "http://example.com/app",
        "http://localhost:5173/app?token=secret",
        "http://localhost:5173/app#private",
        "http://user:password@localhost:5173/app",
        "http://localhost:5173/app\n",
        "http://localhost:0/app",
        "http://localhost:/app",
        "http://localhost:00080/app",
        "http://localhost:bad/app",
        "http://localhost:65536/app",
        "http://localhost:99999/app",
        "http://localhost:5173/bad\\path",
        "http://localhost../app",
        "http://[:::1]:5173/app",
        "http://[0:0:0:0:0:0:0::1]:5173/app",
    ],
)
def test_managed_url_runtime_and_output_schema_reject_unsafe_values(url: str) -> None:
    result = {**start_result_value(), "url": url}
    with pytest.raises(ValidationError):
        FrontendSessionStartResult.model_validate(result)
    facade = {"ok": True, "text": "ready", "result": result}
    validator = Draft202012Validator(schema_for("facade-frontend-session-start-output"))
    assert list(validator.iter_errors(facade))


def test_managed_origin_runtime_and_output_schema_reject_path() -> None:
    result = {**start_result_value(), "base_url": "http://localhost:5173/not-an-origin"}
    with pytest.raises(ValidationError):
        FrontendSessionStartResult.model_validate(result)
    facade = {"ok": True, "text": "ready", "result": result}
    validator = Draft202012Validator(schema_for("facade-frontend-session-start-output"))
    assert list(validator.iter_errors(facade))


def test_snapshot_output_schema_constrains_managed_url() -> None:
    metadata = dict(snapshot_value())
    metadata.pop("image_base64")
    facade = {"ok": True, "text": "captured", "result": metadata}
    validator = Draft202012Validator(schema_for("facade-frontend-snapshot-output"))
    assert not list(validator.iter_errors(facade))
    metadata["url"] = "http://example.com/private?token=secret"
    assert list(validator.iter_errors({**facade, "result": metadata}))


def test_snapshot_carries_validated_png_and_public_metadata_omits_bytes() -> None:
    value = FrontendSnapshotResult.model_validate(snapshot_value())
    assert validate_project_frontend_result(value.model_dump()).snapshot_id == SNAPSHOT
    public = value.model_dump(exclude={"image_base64"})
    assert FrontendSnapshotMetadata.model_validate(public).sha256 == value.sha256
    output_schema = FACADE_OUTPUT_MODELS["frontend_snapshot"].model_json_schema()
    assert "image_base64" not in str(output_schema)
    stored = durable_tool_result({"ok": True, "result": value.model_dump(mode="json")})
    assert stored["error"]["code"] == "outcome_unknown"
    assert "image_base64" not in str(stored)


@pytest.mark.parametrize(
    "mutation",
    [
        {"width": 321},
        {"url": "https://example.com/dashboard"},
        {"url": "http://127.evil.example/dashboard"},
        {
            "elements": [
                {
                    "element_id": "e1",
                    "parent_id": "e2",
                    "tag": "main",
                    "box": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "visible": True,
                }
            ]
        },
    ],
)
def test_snapshot_rejects_mismatched_or_unbound_data(mutation: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FrontendSnapshotResult.model_validate({**snapshot_value(), **mutation})


@pytest.mark.parametrize(
    "mutation",
    [
        {"url": "http://localhost:5173/dashboard?code=secret"},
        {"url": "http://localhost:5173/dashboard#private"},
        {"url": "https://localhost:5173/dashboard"},
    ],
)
def test_snapshot_rejects_unsanitized_page_url(mutation: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FrontendSnapshotResult.model_validate({**snapshot_value(), **mutation})


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:5173/app",
        "http://localhost:5173/?token=secret",
        "http://localhost:5173/#private",
        "https://localhost:5173",
    ],
)
def test_start_result_requires_sanitized_bare_origin(base_url: str) -> None:
    with pytest.raises(ValidationError):
        FrontendSessionStartResult.model_validate(
            {
                "project_id": PROJECT,
                "session_id": SESSION,
                "base_url": base_url,
                "url": "http://localhost:5173/dashboard",
                "viewport": {"width": 320, "height": 240},
                "dev_server": {"ownership": "managed"},
            }
        )


def test_snapshot_bounds_elements_and_redacts_network_urls() -> None:
    with pytest.raises(ValidationError):
        validate_project_frontend(
            {
                "operation": "snapshot",
                "project_id": PROJECT,
                "session_id": SESSION,
                "purpose": "Request too much DOM data",
                "max_elements": 501,
            }
        )
    for url in ("/api/cases?token=secret", "http://localhost:5173/api#private"):
        with pytest.raises(ValidationError):
            FrontendSnapshotResult.model_validate(
                {
                    **snapshot_value(),
                    "network": [{"method": "GET", "url": url, "status": 500}],
                }
            )


def test_source_locations_are_project_relative_and_confidence_is_honest() -> None:
    base = {
        "operation": "source",
        "project_id": PROJECT,
        "session_id": SESSION,
        "snapshot_id": SNAPSHOT,
        "element_id": "e23",
        "confidence": "exact",
    }
    value = FrontendSourceResult.model_validate(
        {
            **base,
            "consumer": {
                "location": {"path": "src/features/CaseActions.tsx", "line": 117},
                "name": "CaseActions",
            },
        }
    )
    assert value.consumer is not None
    assert value.consumer.location.path == "src/features/CaseActions.tsx"
    for path in ("C:/repo/src/App.tsx", "../App.tsx", "/src/App.tsx", ""):
        with pytest.raises(ValidationError):
            FrontendSourceResult.model_validate(
                {
                    **base,
                    "consumer": {"location": {"path": path, "line": 1}},
                }
            )
    with pytest.raises(ValidationError):
        FrontendSourceResult.model_validate(
            {
                **base,
                "confidence": "unavailable",
                "consumer": {"location": {"path": "src/App.tsx", "line": 1}},
            }
        )
    with pytest.raises(ValidationError):
        FrontendSourceResult.model_validate({**base, "confidence": "heuristic"})
    with pytest.raises(ValidationError):
        FrontendSourceResult.model_validate(
            {
                **base,
                "consumer": {"location": {"path": "src/features/CaseActions.tsx", "line": 117}},
                "styles": [{"location": {"path": "src/styles/button.css", "line": 42}}],
            }
        )


def test_matched_css_rule_source_is_reserved_in_v0_3() -> None:
    with pytest.raises(ValidationError):
        FrontendMatchedRule.model_validate(
            {
                "selector": ".button",
                "source": {"path": "src/styles/button.css", "line": 42},
            }
        )


def test_act_result_forces_new_snapshot_before_more_targeting() -> None:
    value = FrontendActResult(
        project_id=PROJECT,
        session_id=SESSION,
        snapshot_id=SNAPSHOT,
        element_id="e23",
        action="hover",
    )
    assert value.snapshot_invalidated is True
