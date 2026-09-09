"""Semantic analysis can execute native toolchains; file read scope is insufficient."""

import pytest
from codito_protocol.envelope import OperationPayload
from codito_protocol.facade import FACADE_MODELS, facade_wire_request
from codito_protocol.facade_contracts import FACADE_CONTRACTS, FACADE_OUTPUT_MODELS

CODE_TOOLS = [name for name in FACADE_MODELS if name.startswith("code_")]


def test_all_sixteen_tools_have_typed_outputs() -> None:
    assert len(CODE_TOOLS) == 16
    assert len(FACADE_MODELS) == 31
    assert set(FACADE_MODELS) == set(FACADE_OUTPUT_MODELS)


@pytest.mark.parametrize("name", CODE_TOOLS)
def test_native_analysis_requires_execution_scope(name: str) -> None:
    scopes = set(FACADE_CONTRACTS[name]["securitySchemes"][0]["scopes"])
    assert {"projects:read", "files:read"} <= scopes
    if name not in {"code_intelligence_status", "code_workspace_summary"}:
        assert "shell:execute" in scopes


def test_new_tool_round_trips_the_tunnel_allowlist() -> None:
    name, payload = facade_wire_request(
        "code_intelligence_status", {"project_id": "test_project_00000001"}
    )
    parsed = OperationPayload(tool_name=name, input=payload)
    assert parsed.tool_name == "project_code"
