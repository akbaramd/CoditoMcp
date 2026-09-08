"""Local-only permission inventory/revocation; never touch the user's real database."""

import sqlite3
from unittest.mock import Mock

import pytest

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.daemon import CoditoDaemon
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode


@pytest.fixture
def permission_database(tmp_path):
    database = AgentDatabase(tmp_path / "saved-permissions.sqlite3")
    for identifier in ("first", "second"):
        database.save_read_permission(identifier, r"C:\sample", "root", "account", "link")
        database.save_shell_permission(identifier, '{"cwd":"C:/sample"}', "root", "account", "link")
        database.save_screen_permission(
            identifier, "monitor", "topology", "Sample screen", "account", "link"
        )
    return database


@pytest.mark.parametrize("category", ["read", "shell", "screen"])
def test_inventory_has_local_id_and_individual_delete_stays_in_category(
    permission_database, category
):
    database = permission_database
    listing = getattr(database, f"list_{category}_permissions")
    assert {item["permission_id"] for item in listing()} == {"first", "second"}
    assert all("permission_key" not in item for item in listing())
    assert database.revoke_permission(category, "first")
    assert [item["permission_id"] for item in listing()] == ["second"]
    assert not database.revoke_permission(category, "first")
    for other in {"read", "shell", "screen"} - {category}:
        assert len(getattr(database, f"list_{other}_permissions")()) == 2


def test_revocation_sql_never_interpolates_category_or_permission_id(permission_database):
    with pytest.raises(AgentError, match="category"):
        permission_database.revoke_permission("read_permissions; DROP TABLE projects", "first")
    assert not permission_database.revoke_permission("read", "' OR 1=1 --")
    assert len(permission_database.list_read_permissions()) == 2


@pytest.fixture
def permission_daemon(permission_database):
    async def deny(_):
        return ApprovalDecision.DENY

    daemon = CoditoDaemon.__new__(CoditoDaemon)
    daemon.database = permission_database
    daemon.approvals = ApprovalManager(deny, permission_database)
    daemon.approval_queue = Mock()
    return daemon


def test_individual_ipc_revocation_advances_generation_and_cancels_pending(permission_daemon):
    daemon = permission_daemon
    generation = daemon.approvals.generation
    request = {"action": "permissions.revoke", "category": "screen", "permission_id": "first"}
    assert daemon._handle_ipc(request) == {"ok": True, "revoked": 1}
    assert daemon.approvals.generation == generation + 1
    daemon.approval_queue.deny_all.assert_called_once()
    assert [item["permission_id"] for item in daemon.database.list_screen_permissions()] == [
        "second"
    ]
    assert daemon._handle_ipc(request) == {"ok": True, "revoked": 0}
    assert daemon.approvals.generation == generation + 1
    daemon.approval_queue.deny_all.assert_called_once()


@pytest.mark.parametrize(
    "extra",
    [
        {"category": "projects"},
        {"category": ["read"]},
        {"permission_id": ""},
        {"permission_id": "x" * 129},
        {"permission_id": "../first"},
        {"permission_id": 1},
        {"approved": True},
        {"account_id": "another-account"},
    ],
)
def test_revoke_ipc_rejects_untyped_or_extra_input(permission_daemon, extra):
    daemon = permission_daemon
    request = {
        "action": "permissions.revoke",
        "category": "read",
        "permission_id": "first",
        **extra,
    }
    assert daemon._handle_ipc(request) == {"ok": False, "error": "invalid_permission"}
    assert daemon.approvals.generation == 0
    daemon.approval_queue.deny_all.assert_not_called()
    assert len(daemon.database.list_read_permissions()) == 2


@pytest.mark.parametrize("with_project_access", [False, True])
def test_access_migration_adds_enum_without_upgrading_legacy_consent(tmp_path, with_project_access):
    path = tmp_path / "legacy.sqlite3"
    modes = "'isolated','native_approval','native_trusted'"
    if with_project_access:
        modes += ",'native_project'"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE projects (project_id TEXT PRIMARY KEY,title TEXT NOT NULL,"
            "root TEXT NOT NULL UNIQUE,root_fingerprint TEXT NOT NULL,"
            f"mode TEXT NOT NULL CHECK(mode IN ({modes})),enabled INTEGER NOT NULL DEFAULT 1,"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        for mode in ("isolated", "native_trusted"):
            connection.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,1,'old','old')",
                (mode, mode, str(tmp_path / mode), "same-root-id", mode),
            )
    database = AgentDatabase(path)
    assert database.get_project("isolated").mode is ProjectMode.ISOLATED
    assert database.get_project("native_trusted").mode is ProjectMode.NATIVE_TRUSTED
    assert database.get_project("native_trusted").root_fingerprint == "same-root-id"
    database.set_project_mode("isolated", ProjectMode.FULL_ACCESS)
    assert database.get_project("isolated").mode is ProjectMode.FULL_ACCESS
    reopened = AgentDatabase(path)
    assert reopened.get_project("isolated").mode is ProjectMode.FULL_ACCESS
    assert reopened.get_project("native_trusted").mode is ProjectMode.NATIVE_TRUSTED


def test_new_full_access_ipc_requires_ack_and_invalidates_pending(permission_daemon):
    daemon = permission_daemon
    daemon.database = Mock()
    daemon._project_metadata_changed = Mock()
    payload = {"action": "project.mode", "project_id": "sample", "mode": "full_access"}
    with pytest.raises(AgentError, match="explicitly"):
        daemon._handle_ipc(payload)
    daemon.database.set_project_mode.assert_not_called()
    assert daemon.approvals.generation == 0
    assert daemon._handle_ipc({**payload, "acknowledge_full_user_authority": True}) == {"ok": True}
    daemon.database.set_project_mode.assert_called_once_with("sample", ProjectMode.FULL_ACCESS)
    assert daemon.approvals.generation == 1
    daemon.approval_queue.deny_all.assert_called_once()
    daemon._project_metadata_changed.assert_called_once()
