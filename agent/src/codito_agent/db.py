from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import AgentError
from .models import TERMINAL_OPERATION_STATES, OperationState, Project, ProjectMode
from .paths import validate_project_root


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentDatabase:
    """Local source of truth for roots, receipts, idempotency, and connection epochs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_lock = threading.Lock()
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._migration_lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    root TEXT NOT NULL UNIQUE,
                    root_fingerprint TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(
                        mode IN ('isolated','native_approval','native_trusted')
                    ),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    correlation_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    link_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    project_id TEXT,
                    capability TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    idempotency_key TEXT,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    terminal_acked INTEGER NOT NULL DEFAULT 0,
                    connection_epoch INTEGER NOT NULL,
                    deadline_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );
                CREATE INDEX IF NOT EXISTS operations_state_idx ON operations(state, updated_at);
                CREATE TABLE IF NOT EXISTS idempotency (
                    project_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    link_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, capability, idempotency_key),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS read_permissions (
                    permission_key TEXT PRIMARY KEY,
                    scope_path TEXT NOT NULL,
                    root_identity TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    link_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shell_permissions (
                    permission_key TEXT PRIMARY KEY,
                    scope_path TEXT NOT NULL,
                    root_identity TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    link_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_registration_requests (
                    request_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    link_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    connection_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('pending','completed','dismissed','expired')
                    ),
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS project_registration_pending_idx
                    ON project_registration_requests(status, created_at);
                """
            )
            # Rebuild only the CHECK constraint; preserve IDs and referencing journals.
            schema = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='projects'"
                ).fetchone()[0]
            )
            if "native_project" not in schema:
                connection.execute("PRAGMA foreign_keys=OFF")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        schema.replace("projects", "projects_v2", 1).replace(
                            "'native_trusted'", "'native_trusted','native_project'"
                        )
                    )
                    connection.execute("INSERT INTO projects_v2 SELECT * FROM projects")
                    connection.execute("DROP TABLE projects")
                    connection.execute("ALTER TABLE projects_v2 RENAME TO projects")
                    if connection.execute("PRAGMA foreign_key_check").fetchone():
                        raise AgentError("migration_failed", "Project references failed validation")
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                finally:
                    connection.execute("PRAGMA foreign_keys=ON")
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(operations)").fetchall()
            }
            if "link_id" not in columns:
                connection.execute(
                    "ALTER TABLE operations ADD COLUMN link_id TEXT NOT NULL DEFAULT ''"
                )
            if "terminal_acked" not in columns:
                connection.execute(
                    "ALTER TABLE operations ADD COLUMN terminal_acked INTEGER NOT NULL DEFAULT 0"
                )
            idempotency_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(idempotency)").fetchall()
            }
            for name in ("account_id", "grant_id", "link_id", "device_id"):
                if name not in idempotency_columns:
                    connection.execute(
                        f"ALTER TABLE idempotency ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )

    def register_project(
        self,
        title: str,
        root: Path,
        mode: ProjectMode = ProjectMode.ISOLATED,
        *,
        project_id: str | None = None,
    ) -> Project:
        title = title.strip()
        if not title or len(title) > 120:
            raise AgentError("invalid_project", "Project title must contain 1 to 120 characters")
        validated_root, fingerprint = validate_project_root(root)
        project = Project(project_id or str(uuid.uuid4()), title, validated_root, fingerprint, mode)
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO projects
                       (project_id,title,root,root_fingerprint,mode,enabled,created_at,updated_at)
                       VALUES (?,?,?,?,?,1,?,?)""",
                    (
                        project.project_id,
                        project.title,
                        str(project.root),
                        project.root_fingerprint,
                        project.mode.value,
                        now,
                        now,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise AgentError("project_exists", "That project root is already registered") from exc
        return project

    def get_project(self, project_id: str) -> Project:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
        if row is None:
            raise AgentError("project_not_found", "The requested project is not registered")
        return self._project_from_row(row)

    def list_projects(self, *, enabled_only: bool = True) -> list[Project]:
        query = "SELECT * FROM projects"
        params: tuple[object, ...] = ()
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY title COLLATE NOCASE, project_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._project_from_row(row) for row in rows]

    def set_project_mode(self, project_id: str, mode: ProjectMode) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE projects SET mode=?,updated_at=? WHERE project_id=?",
                (mode.value, _now(), project_id),
            )
        if cursor.rowcount != 1:
            raise AgentError("project_not_found", "The requested project is not registered")

    def set_project_title(self, project_id: str, title: str) -> None:
        title = title.strip()
        if not title or len(title) > 120:
            raise AgentError("invalid_project", "Project title must contain 1 to 120 characters")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE projects SET title=?,updated_at=? WHERE project_id=?",
                (title, _now(), project_id),
            )
        if cursor.rowcount != 1:
            raise AgentError("project_not_found", "The requested project is not registered")

    def set_project_enabled(self, project_id: str, *, enabled: bool) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE projects SET enabled=?,updated_at=? WHERE project_id=?",
                (int(enabled), _now(), project_id),
            )
        if cursor.rowcount != 1:
            raise AgentError("project_not_found", "The requested project is not registered")

    def rotate_project_ids(self) -> dict[str, str]:
        """Give every local project a new device-scoped opaque identifier.

        A revoked device can never reclaim its relay identity.  Re-enrollment
        therefore creates a new device and must also rotate the project ids
        advertised by that device.  Roots, titles, modes, and local operation
        history are preserved; old idempotency grants are deliberately cleared.
        """

        with self._connect() as connection:
            rows = connection.execute("SELECT project_id FROM projects").fetchall()
            mapping = {str(row["project_id"]): str(uuid.uuid4()) for row in rows}
            if not mapping:
                return mapping
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA defer_foreign_keys=ON")
            try:
                for previous, current in mapping.items():
                    connection.execute(
                        "UPDATE operations SET project_id=? WHERE project_id=?",
                        (current, previous),
                    )
                    connection.execute(
                        "UPDATE projects SET project_id=?,updated_at=? WHERE project_id=?",
                        (current, _now(), previous),
                    )
                connection.execute("DELETE FROM idempotency")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return mapping

    def disable_project(self, project_id: str) -> None:
        self.set_project_enabled(project_id, enabled=False)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def has_read_permission(self, permission_key: str, root_identity: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM read_permissions WHERE permission_key=? AND root_identity=?",
                (permission_key, root_identity),
            ).fetchone()
        return row is not None

    def save_read_permission(
        self,
        permission_key: str,
        scope_path: str,
        root_identity: str,
        account_id: str,
        link_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO read_permissions
                   (permission_key,scope_path,root_identity,account_id,link_id,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (permission_key, scope_path, root_identity, account_id, link_id, _now()),
            )

    def list_read_permissions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT scope_path,account_id,link_id,created_at FROM read_permissions "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_read_permissions(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM read_permissions")
            return cursor.rowcount

    def has_shell_permission(self, key: str, identity: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM shell_permissions WHERE permission_key=? AND root_identity=?",
                    (key, identity),
                ).fetchone()
                is not None
            )

    def save_shell_permission(
        self, key: str, scope: str, identity: str, account: str, link: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO shell_permissions VALUES (?,?,?,?,?,?)",
                (key, scope, identity, account, link, _now()),
            )

    def list_shell_permissions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT scope_path,account_id,link_id,created_at FROM shell_permissions "
                    "ORDER BY created_at DESC"
                )
            ]

    def revoke_shell_permissions(self) -> int:
        with self._connect() as connection:
            return connection.execute("DELETE FROM shell_permissions").rowcount

    def set_setting(self, key: str, value: str) -> None:
        if not key or len(key) > 100 or len(value) > 4096:
            raise AgentError("invalid_setting", "Setting key or value is outside its limit")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO settings(key,value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def create_project_registration_request(
        self,
        *,
        title: str,
        account_id: str,
        grant_id: str,
        link_id: str,
        device_id: str,
        connection_epoch: int,
    ) -> str:
        title = title.strip()
        if not title or len(title) > 120:
            raise AgentError("invalid_project", "Project title must contain 1 to 120 characters")
        request_id = f"project_request_{uuid.uuid4().hex}"
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO project_registration_requests
                   (request_id,title,account_id,grant_id,link_id,device_id,connection_epoch,
                    status,expires_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,'pending',?,?,?)""",
                (
                    request_id,
                    title,
                    account_id,
                    grant_id,
                    link_id,
                    device_id,
                    connection_epoch,
                    (now + timedelta(minutes=15)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return request_id

    def next_project_registration_request(self) -> dict[str, Any] | None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """UPDATE project_registration_requests SET status='expired',updated_at=?
                   WHERE status='pending' AND expires_at<=?""",
                (now, now),
            )
            row = connection.execute(
                """SELECT request_id,title,account_id,device_id,expires_at
                   FROM project_registration_requests WHERE status='pending'
                   ORDER BY created_at LIMIT 1"""
            ).fetchone()
        return dict(row) if row is not None else None

    def complete_project_registration_request(self, request_id: str, root: Path) -> Project:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT title,expires_at FROM project_registration_requests
                   WHERE request_id=? AND status='pending'""",
                (request_id,),
            ).fetchone()
        if row is None:
            raise AgentError("request_not_found", "Project registration request is unavailable")
        if str(row["expires_at"]) <= _now():
            self.dismiss_project_registration_request(request_id, expired=True)
            raise AgentError("request_expired", "Project registration request has expired")
        project = self.register_project(str(row["title"]), root)
        with self._connect() as connection:
            connection.execute(
                """UPDATE project_registration_requests SET status='completed',updated_at=?
                   WHERE request_id=? AND status='pending'""",
                (_now(), request_id),
            )
        return project

    def dismiss_project_registration_request(
        self, request_id: str, *, expired: bool = False
    ) -> None:
        status = "expired" if expired else "dismissed"
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE project_registration_requests SET status=?,updated_at=?
                   WHERE request_id=? AND status='pending'""",
                (status, _now(), request_id),
            )
        if cursor.rowcount != 1:
            raise AgentError("request_not_found", "Project registration request is unavailable")

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            project_id=str(row["project_id"]),
            title=str(row["title"]),
            root=Path(str(row["root"])),
            root_fingerprint=str(row["root_fingerprint"]),
            mode=ProjectMode(str(row["mode"])),
            enabled=bool(row["enabled"]),
        )

    def record_received(
        self,
        *,
        operation_id: str,
        correlation_id: str,
        account_id: str,
        grant_id: str,
        link_id: str,
        device_id: str,
        project_id: str | None,
        capability: str,
        action_digest: str,
        idempotency_key: str | None,
        request_digest: str,
        connection_epoch: int,
        deadline_at: str,
    ) -> bool:
        """Persist a request before receipt acknowledgement.

        Returns ``False`` for an already persisted operation, allowing redelivery
        to be acknowledged without executing it twice.
        """

        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO operations
                       (operation_id,correlation_id,account_id,grant_id,link_id,device_id,project_id,
                        capability,action_digest,idempotency_key,request_digest,state,result_json,
                        connection_epoch,deadline_at,received_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",
                    (
                        operation_id,
                        correlation_id,
                        account_id,
                        grant_id,
                        link_id,
                        device_id,
                        project_id,
                        capability,
                        action_digest,
                        idempotency_key,
                        request_digest,
                        OperationState.RECEIVED.value,
                        connection_epoch,
                        deadline_at,
                        now,
                        now,
                    ),
                )
                connection.commit()
            return True
        except sqlite3.IntegrityError:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT correlation_id,account_id,grant_id,link_id,device_id,project_id,
                              capability,action_digest,idempotency_key,request_digest,
                              connection_epoch,deadline_at
                       FROM operations WHERE operation_id=?""",
                    (operation_id,),
                ).fetchone()
            expected = (
                correlation_id,
                account_id,
                grant_id,
                link_id,
                device_id,
                project_id,
                capability,
                action_digest,
                idempotency_key,
                request_digest,
                deadline_at,
            )
            if row is None:
                raise AgentError(
                    "operation_conflict", "Operation identifier was reused with different bindings"
                ) from None
            actual = tuple(
                row[name]
                for name in (
                    "correlation_id",
                    "account_id",
                    "grant_id",
                    "link_id",
                    "device_id",
                    "project_id",
                    "capability",
                    "action_digest",
                    "idempotency_key",
                    "request_digest",
                    "deadline_at",
                )
            )
            if actual != expected or connection_epoch < int(row["connection_epoch"]):
                raise AgentError(
                    "operation_conflict", "Operation identifier was reused with different bindings"
                ) from None
            if connection_epoch > int(row["connection_epoch"]):
                with self._connect() as connection:
                    connection.execute(
                        """UPDATE operations SET connection_epoch=?,updated_at=?
                           WHERE operation_id=?""",
                        (connection_epoch, _now(), operation_id),
                    )
            return False

    def transition_operation(
        self,
        operation_id: str,
        state: OperationState,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise AgentError("operation_not_found", "Operation receipt is missing")
            old = OperationState(str(row["state"]))
            if old in TERMINAL_OPERATION_STATES and old != state:
                connection.rollback()
                raise AgentError("invalid_state", "A terminal operation cannot transition")
            connection.execute(
                """UPDATE operations
                   SET state=?,result_json=?,
                       terminal_acked=CASE WHEN ? THEN 0 ELSE terminal_acked END,
                       updated_at=?
                   WHERE operation_id=?""",
                (
                    state.value,
                    json.dumps(result, separators=(",", ":"), sort_keys=True)
                    if result is not None
                    else None,
                    int(state in TERMINAL_OPERATION_STATES),
                    _now(),
                    operation_id,
                ),
            )
            connection.commit()

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json")) if value["result_json"] else None
        return value

    def list_unacknowledged_terminals(self) -> list[dict[str, Any]]:
        """Return durable terminal results that the relay has not acknowledged."""

        states = tuple(state.value for state in TERMINAL_OPERATION_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM operations
                     WHERE terminal_acked=0 AND result_json IS NOT NULL
                       AND state IN (?,?,?,?,?)
                     ORDER BY updated_at, operation_id""",
                states,
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["result"] = json.loads(value.pop("result_json"))
            values.append(value)
        return values

    def list_recent_operations(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return a bounded, display-safe activity feed for the local desktop UI."""

        if not 1 <= limit <= 200:
            raise AgentError("invalid_request", "Activity limit must be between 1 and 200")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT operations.operation_id,operations.project_id,
                          projects.title AS project_title,operations.capability,
                          operations.state,operations.result_json,
                          operations.terminal_acked,operations.received_at,
                          operations.updated_at
                     FROM operations
                     LEFT JOIN projects ON projects.project_id=operations.project_id
                    ORDER BY operations.updated_at DESC,operations.operation_id DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
        activity: list[dict[str, Any]] = []
        for row in rows:
            result = json.loads(row["result_json"]) if row["result_json"] else None
            error_code = None
            if isinstance(result, dict) and isinstance(result.get("error"), dict):
                code = result["error"].get("code")
                error_code = str(code) if code else None
            activity.append(
                {
                    "operation_id": str(row["operation_id"]),
                    "project_id": str(row["project_id"]) if row["project_id"] else None,
                    "project_title": str(row["project_title"] or "Device"),
                    "capability": str(row["capability"]),
                    "state": str(row["state"]),
                    "error_code": error_code,
                    "terminal_acked": bool(row["terminal_acked"]),
                    "received_at": str(row["received_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
        return activity

    def acknowledge_terminal(
        self,
        *,
        correlation_id: str,
        action_digest: str,
        account_id: str,
        grant_id: str,
        link_id: str,
        device_id: str,
        project_id: str | None,
    ) -> bool:
        """Mark only an exactly bound terminal result as durably acknowledged."""

        states = tuple(state.value for state in TERMINAL_OPERATION_STATES)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE operations SET terminal_acked=1,updated_at=?
                     WHERE correlation_id=? AND action_digest=? AND account_id=? AND grant_id=?
                       AND link_id=? AND device_id=? AND project_id IS ?
                       AND state IN (?,?,?,?,?)""",
                (
                    _now(),
                    correlation_id,
                    action_digest,
                    account_id,
                    grant_id,
                    link_id,
                    device_id,
                    project_id,
                    *states,
                ),
            )
        return cursor.rowcount > 0

    def get_idempotency(
        self,
        project_id: str,
        capability: str,
        key: str,
        *,
        account_id: str,
        grant_id: str,
        link_id: str,
        device_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT account_id,grant_id,link_id,device_id,
                          request_digest,state,result_json FROM idempotency
                   WHERE project_id=? AND capability=? AND idempotency_key=?""",
                (project_id, capability, key),
            ).fetchone()
        if row is None:
            return None
        if tuple(row[name] for name in ("account_id", "grant_id", "link_id", "device_id")) != (
            account_id,
            grant_id,
            link_id,
            device_id,
        ):
            raise AgentError(
                "idempotency_conflict",
                "Idempotency key belongs to a different authorization binding",
            )
        return {
            "request_digest": str(row["request_digest"]),
            "state": str(row["state"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def put_idempotency(
        self,
        project_id: str,
        capability: str,
        key: str,
        request_digest: str,
        state: OperationState,
        result: Mapping[str, Any] | None = None,
        *,
        account_id: str,
        grant_id: str,
        link_id: str,
        device_id: str,
    ) -> None:
        now = _now()
        encoded = json.dumps(result, separators=(",", ":"), sort_keys=True) if result else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT request_digest,account_id,grant_id,link_id,device_id FROM idempotency
                   WHERE project_id=? AND capability=? AND idempotency_key=?""",
                (project_id, capability, key),
            ).fetchone()
            if row is not None and (
                str(row["request_digest"]) != request_digest
                or tuple(row[name] for name in ("account_id", "grant_id", "link_id", "device_id"))
                != (account_id, grant_id, link_id, device_id)
            ):
                connection.rollback()
                raise AgentError(
                    "idempotency_conflict", "Idempotency key was reused for a different request"
                )
            connection.execute(
                """INSERT INTO idempotency
                   (project_id,capability,idempotency_key,account_id,grant_id,link_id,device_id,
                    request_digest,state,result_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id,capability,idempotency_key) DO UPDATE SET
                     state=excluded.state,result_json=excluded.result_json,updated_at=excluded.updated_at""",
                (
                    project_id,
                    capability,
                    key,
                    account_id,
                    grant_id,
                    link_id,
                    device_id,
                    request_digest,
                    state.value,
                    encoded,
                    now,
                    now,
                ),
            )
            connection.commit()

    def delete_idempotency(self, project_id: str, capability: str, key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM idempotency WHERE project_id=? AND capability=? AND idempotency_key=?",
                (project_id, capability, key),
            )

    def next_connection_epoch(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM settings WHERE key='connection_epoch'"
            ).fetchone()
            value = int(row["value"]) + 1 if row else 1
            connection.execute(
                """INSERT INTO settings(key,value) VALUES('connection_epoch',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(value),),
            )
            connection.commit()
        return value

    def set_connection_epoch(self, value: int) -> None:
        if value < 1:
            raise AgentError("protocol_error", "Connection epoch must be positive")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO settings(key,value) VALUES('connection_epoch',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(value),),
            )
