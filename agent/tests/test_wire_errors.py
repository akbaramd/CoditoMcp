from __future__ import annotations

import ast
from pathlib import Path

from codito_protocol import ErrorCode, ToolFailure

from codito_agent.errors import AgentError
from codito_agent.wire_errors import LOCAL_ERROR_MAP, to_tool_failure


def test_every_literal_agent_error_code_has_a_wire_mapping() -> None:
    source_root = Path(__file__).parents[1] / "src" / "codito_agent"
    codes: set[str] = set()
    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                codes.add(node.args[0].value)
    assert codes <= LOCAL_ERROR_MAP.keys()
    assert all(isinstance(value, ErrorCode) for value in LOCAL_ERROR_MAP.values())


def test_wire_failure_is_typed_and_redacts_absolute_paths() -> None:
    failure = to_tool_failure(
        AgentError("path_race", "Path changed", {"debug": r"C:\secret\file.txt"}),
        "correlation_abcdefgh",
    )
    validated = ToolFailure.model_validate(failure.model_dump())
    assert validated.error.code is ErrorCode.PATH_CHANGED
    assert validated.error.details["debug"] == "[local path redacted]"
