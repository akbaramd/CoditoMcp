from __future__ import annotations

from pathlib import Path

import pytest

from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.models import OperationState, ProjectMode


def test_project_registry_never_exposes_root_in_public_metadata(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    assert project.root == project_root.resolve()
    assert "root" not in project.public_dict()
    assert database.get_project(project.project_id) == project
    database.set_project_mode(project.project_id, ProjectMode.NATIVE_APPROVAL)
    assert database.get_project(project.project_id).mode is ProjectMode.NATIVE_APPROVAL


def test_operation_receipt_is_durable_and_conflicting_reuse_is_rejected(tmp_path: Path) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    values = {
        "operation_id": "message_abcdefghijkl",
        "correlation_id": "correlation_abcdefgh",
        "account_id": "account_abcdefghijkl",
        "grant_id": "grant_abcdefghijklmn",
        "link_id": "link_abcdefghijklmnop",
        "device_id": "device_abcdefghijkl",
        "project_id": None,
        "capability": "project_read",
        "action_digest": "a" * 64,
        "idempotency_key": None,
        "request_digest": "b" * 64,
        "connection_epoch": 1,
        "deadline_at": "2030-01-01T00:00:00+00:00",
    }
    assert database.record_received(**values)
    assert not database.record_received(**values)
    assert not database.record_received(**{**values, "connection_epoch": 2})
    assert database.get_operation(values["operation_id"])["connection_epoch"] == 2
    with pytest.raises(AgentError):
        database.record_received(**{**values, "connection_epoch": 1})
    database.transition_operation(values["operation_id"], OperationState.SUCCEEDED, {"ok": True})
    operation = database.get_operation(values["operation_id"])
    assert operation is not None and operation["result"] == {"ok": True}
    assert [item["operation_id"] for item in database.list_unacknowledged_terminals()] == [
        values["operation_id"]
    ]
    assert database.acknowledge_terminal(
        correlation_id=values["correlation_id"],
        action_digest=values["action_digest"],
        account_id=values["account_id"],
        grant_id=values["grant_id"],
        link_id=values["link_id"],
        device_id=values["device_id"],
        project_id=None,
    )
    assert database.list_unacknowledged_terminals() == []
    with pytest.raises(AgentError, match="reused"):
        database.record_received(**{**values, "request_digest": "c" * 64})
    with pytest.raises(AgentError, match="bindings"):
        database.record_received(**{**values, "account_id": "account_different000"})
