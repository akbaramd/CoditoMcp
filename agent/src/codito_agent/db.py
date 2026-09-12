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

_ACTIVITY_SECRET_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)


def _activity_safe_value(value: Any, *, depth: int = 0) -> Any:
    """Create a JSON-safe local audit value without transport credentials."""

    if depth > 12:
        return "[maximum nesting reached]"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            normalized = name.casefold().replace("-", "_")
            safe[name] = (
                "[redacted]"
                if normalized in _ACTIVITY_SECRET_KEYS
                else _activity_safe_value(item, depth=depth + 1)
            )
        return safe
    if isinstance(value, (list, tuple)):
        return [_activity_safe_value(item, depth=depth + 1) for item in value]
    return str(value)


def _json_object(value: object) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _activity_target(request: Mapping[str, Any] | None) -> str | None:
    if request is None:
        return None
    for key in ("path", "working_directory", "scope_path", "display_id", "title", "query"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:240]
    patch = request.get("patch")
    if isinstance(patch, str):
        paths = [
            line.split(":", 1)[1].strip()
            for line in patch.splitlines()
            if line.startswith(
                ("*** Add File:", "*** Update File:", "*** Delete File:", "*** Move to:")
            )
            and ":" in line
        ]
        if paths:
            visible = ", ".join(paths[:3])
            return f"{visible}{' …' if len(paths) > 3 else ''}"[:240]
    command = request.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip().replace("\r", " ").replace("\n", " ")[:240]
    return None


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
                        mode IN (
                            'isolated','native_approval','native_trusted','native_project','full_access'
                        )
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
                    request_json TEXT,
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
                    job_id TEXT,
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
                CREATE TABLE IF NOT EXISTS screen_permissions (
                    permission_key TEXT PRIMARY KEY,
                    display_id TEXT NOT NULL,
                    display_identity TEXT NOT NULL,
                    display_label TEXT NOT NULL,
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
            missing_modes = [
                mode for mode in ("native_project", "full_access") if f"'{mode}'" not in schema
            ]
            if missing_modes:
                connection.execute("PRAGMA foreign_keys=OFF")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        schema.replace("projects", "projects_v3", 1).replace(
                            "'native_trusted'",
                            "'native_trusted'," + ",".join(f"'{mode}'" for mode in missing_modes),
                        )
                    )
                    connection.execute("INSERT INTO projects_v3 SELECT * FROM projects")
                    connection.execute("DROP TABLE projects")
                    connection.execute("ALTER TABLE projects_v3 RENAME TO projects")
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
            if "request_json" not in columns:
                connection.execute("ALTER TABLE operations ADD COLUMN request_json TEXT")
            idempotency_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(idempotency)").fetchall()
            }
            for name in ("account_id", "grant_id", "link_id", "device_id"):
                if name not in idempotency_columns:
                    connection.execute(
                        f"ALTER TABLE idempotency ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            if "job_id" not in idempotency_columns:
                connection.execute("ALTER TABLE idempotency ADD COLUMN job_id TEXT")
                rows = connection.execute(
                    "SELECT rowid,result_json FROM idempotency "
                    "WHERE capability='project_shell' AND result_json IS NOT NULL"
                ).fetchall()
                recovered_job_ids: list[tuple[str, int]] = []
                for row in rows:
                    try:
                        result = json.loads(str(row["result_json"]))
                    except (TypeError, ValueError):
                        continue
                    job_id = result.get("job_id") if isinstance(result, dict) else None
                    if isinstance(job_id, str) and job_id:
                        recovered_job_ids.append((job_id, int(row["rowid"])))
                connection.executemany(
                    "UPDATE idempotency SET job_id=? WHERE rowid=?", recovered_job_ids
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idempotency_shell_job_idx "
                "ON idempotency(project_id,capability,job_id) WHERE job_id IS NOT NULL"
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
                "SELECT permission_key AS permission_id,scope_path,account_id,link_id,created_at "
                "FROM read_permissions "
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
                    "SELECT permission_key AS permission_id,scope_path,account_id,"
                    "link_id,created_at "
                    "FROM shell_permissions "
                    "ORDER BY created_at DESC"
                )
            ]

    def revoke_shell_permissions(self) -> int:
        with self._connect() as connection:
            return connection.execute("DELETE FROM shell_permissions").rowcount

    def has_screen_permission(self, key: str, identity: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM screen_permissions "
                    "WHERE permission_key=? AND display_identity=?",
                    (key, identity),
                ).fetchone()
                is not None
            )

    def save_screen_permission(
        self, key: str, display_id: str, identity: str, label: str, account: str, link: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO screen_permissions VALUES (?,?,?,?,?,?,?)",
                (key, display_id, identity, label, account, link, _now()),
            )

    def list_screen_permissions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT permission_key AS permission_id,display_id,display_label,"
                    "account_id,link_id,created_at "
                    "FROM screen_permissions ORDER BY created_at DESC"
                )
            ]

    def revoke_screen_permissions(self) -> int:
        with self._connect() as connection:
            return connection.execute("DELETE FROM screen_permissions").rowcount

    def revoke_permission(self, category: str, permission_id: str) -> bool:
        """Remove one local grant; category never becomes an SQL identifier."""
        statements = {
            "read": "DELETE FROM read_permissions WHERE permission_key=?",
            "shell": "DELETE FROM shell_permissions WHERE permission_key=?",
            "screen": "DELETE FROM screen_permissions WHERE permission_key=?",
        }
        statement = statements.get(category)
        if statement is None:
            raise AgentError("invalid_permission", "Unknown saved permission category")
        with self._connect() as connection:
            return connection.execute(statement, (permission_id,)).rowcount == 1

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
        request: Mapping[str, Any] | None = None,
        connection_epoch: int,
        deadline_at: str,
    ) -> bool:
        """Persist a request before receipt acknowledgement.

        Returns ``False`` for an already persisted operation, allowing redelivery
        to be acknowledged without executing it twice.
        """

        now = _now()
        request_json = (
            json.dumps(
                _activity_safe_value(request),
                separators=(",", ":"),
                sort_keys=True,
                ensure_ascii=False,
            )
            if request is not None
            else None
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO operations
                       (operation_id,correlation_id,account_id,grant_id,link_id,device_id,project_id,
                        capability,action_digest,idempotency_key,request_digest,request_json,state,
                        result_json,connection_epoch,deadline_at,received_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",
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
                        request_json,
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
                          operations.state,operations.request_json,operations.result_json,
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
            request = _json_object(row["request_json"])
            result = _json_object(row["result_json"])
            error_code = None
            error_message = None
            if isinstance(result, dict) and isinstance(result.get("error"), dict):
                code = result["error"].get("code")
                error_code = str(code) if code else None
                message = result["error"].get("message")
                error_message = str(message) if message else None
            operation = None
            purpose = None
            if request is not None:
                action = request.get("operation", request.get("action"))
                operation = str(action) if action else None
                raw_purpose = request.get("purpose")
                purpose = str(raw_purpose) if raw_purpose else None
            if operation is None and isinstance(result, dict):
                result_value = result.get("result")
                if isinstance(result_value, dict):
                    action = result_value.get("operation", result_value.get("action"))
                    operation = str(action) if action else None
            activity.append(
                {
                    "operation_id": str(row["operation_id"]),
                    "project_id": str(row["project_id"]) if row["project_id"] else None,
                    "project_title": str(row["project_title"] or "Device"),
                    "capability": str(row["capability"]),
                    "state": str(row["state"]),
                    "error_code": error_code,
                    "error_message": error_message,
                    "operation": operation,
                    "purpose": purpose,
                    "target": _activity_target(request),
                    "terminal_acked": bool(row["terminal_acked"]),
                    "received_at": str(row["received_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
        return activity

    def get_activity_detail(self, operation_id: str) -> dict[str, Any] | None:
        """Return one complete local audit record for the authenticated desktop UI."""

        if not operation_id or len(operation_id) > 200:
            raise AgentError("invalid_request", "Activity operation identifier is invalid")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT operations.*,projects.title AS project_title
                     FROM operations
                     LEFT JOIN projects ON projects.project_id=operations.project_id
                    WHERE operations.operation_id=?""",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        request = _json_object(row["request_json"])
        result = _json_object(row["result_json"])
        error = result.get("error") if isinstance(result, dict) else None
        operation = None
        if request is not None:
            action = request.get("operation", request.get("action"))
            operation = str(action) if action else None
        if operation is None and isinstance(result, dict):
            result_value = result.get("result")
            if isinstance(result_value, dict):
                action = result_value.get("operation", result_value.get("action"))
                operation = str(action) if action else None
        try:
            received = datetime.fromisoformat(str(row["received_at"]))
            updated = datetime.fromisoformat(str(row["updated_at"]))
            duration_ms = max(0, int((updated - received).total_seconds() * 1000))
        except ValueError:
            duration_ms = None
        return {
            "operation_id": str(row["operation_id"]),
            "correlation_id": str(row["correlation_id"]),
            "account_id": str(row["account_id"]),
            "grant_id": str(row["grant_id"]),
            "link_id": str(row["link_id"]),
            "device_id": str(row["device_id"]),
            "project_id": str(row["project_id"]) if row["project_id"] else None,
            "project_title": str(row["project_title"] or "Device"),
            "capability": str(row["capability"]),
            "operation": operation,
            "state": str(row["state"]),
            "action_digest": str(row["action_digest"]),
            "idempotency_key": (str(row["idempotency_key"]) if row["idempotency_key"] else None),
            "request_digest": str(row["request_digest"]),
            "connection_epoch": int(row["connection_epoch"]),
            "deadline_at": str(row["deadline_at"]),
            "received_at": str(row["received_at"]),
            "updated_at": str(row["updated_at"]),
            "duration_ms": duration_ms,
            "terminal_acked": bool(row["terminal_acked"]),
            "request": request,
            "result": result,
            "error": error if isinstance(error, dict) else None,
        }

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
        job_id_value = result.get("job_id") if capability == "project_shell" and result else None
        job_id = job_id_value if isinstance(job_id_value, str) and job_id_value else None
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
                    request_digest,state,result_json,job_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id,capability,idempotency_key) DO UPDATE SET
                      state=excluded.state,result_json=excluded.result_json,job_id=excluded.job_id,
                      updated_at=excluded.updated_at""",
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
                    job_id,
                    now,
                    now,
                ),
            )
            connection.commit()

    def get_shell_job(
        self,
        project_id: str,
        job_id: str,
        *,
        account_id: str,
        grant_id: str,
        link_id: str,
        device_id: str,
    ) -> dict[str, Any] | None:
        """Resolve a shell job by its public job ID after in-memory eviction or restart."""

        with self._connect() as connection:
            row = connection.execute(
                """SELECT account_id,grant_id,link_id,device_id,state,result_json
                   FROM idempotency
                   WHERE project_id=? AND capability='project_shell' AND job_id=?""",
                (project_id, job_id),
            ).fetchone()
        if row is None:
            return None
        if tuple(row[name] for name in ("account_id", "grant_id", "link_id", "device_id")) != (
            account_id,
            grant_id,
            link_id,
            device_id,
        ):
            raise AgentError("binding_mismatch", "Shell job authorization binding does not match")
        return {
            "state": str(row["state"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

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
