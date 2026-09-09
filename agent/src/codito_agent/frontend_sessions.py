from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .db import AgentDatabase
from .dev_servers import DevServerHandle, DevServerManager, FrontendLaunchConfig
from .errors import AgentError
from .frontend_playwright import BrowserSnapshot, ManagedPage
from .models import Project, ProjectMode
from .paths import ProjectPathResolver, validate_relative_path
from .read_tools import ToolResponse

MAX_SESSIONS = 8
MAX_SOURCE_BYTES = 4 * 1024 * 1024
SESSION_MAX = timedelta(minutes=30)
SESSION_IDLE = timedelta(minutes=10)
TOMBSTONE_MAX = 128
# Playwright may spend up to 30 seconds on each of the bundled-browser and
# source-only Edge fallback launches, followed by a 30-second initial goto.
# Reserve that worst case plus a small handoff margin before showing approval.
FRONTEND_BROWSER_RESERVE = timedelta(seconds=95)
MIN_FRONTEND_APPROVAL_WINDOW = timedelta(seconds=5)


def _as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


class BrowserBackend(Protocol):
    async def open(
        self,
        *,
        profile_directory: Path,
        url: str,
        allowed_origin: str,
        viewport: tuple[int, int],
    ) -> ManagedPage: ...

    async def snapshot(self, record: ManagedPage, *, max_elements: int) -> BrowserSnapshot: ...

    async def inspect(
        self,
        record: ManagedPage,
        *,
        backend_node_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> dict[str, Any]: ...

    async def source_attributes(
        self, record: ManagedPage, backend_node_id: int
    ) -> dict[str, str]: ...

    async def act(
        self,
        record: ManagedPage,
        *,
        backend_node_id: int,
        action: str,
        text: str | None = None,
        key: str | None = None,
        option: str | None = None,
        delta_x: float = 0,
        delta_y: float = 0,
        expected_document_token: str,
    ) -> None: ...

    async def close_page(self, record: ManagedPage) -> None: ...

    def current_url(self, record: ManagedPage) -> str: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class FrontendSession:
    session_id: str
    project_id: str
    project_root: Path
    root_fingerprint: str
    project_mode: ProjectMode
    grant_id: str
    link_id: str
    connection_epoch: int
    security_generation: int
    config: FrontendLaunchConfig
    server: DevServerHandle
    browser: ManagedPage
    viewport: tuple[int, int]
    public_url: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    snapshot_id: str | None = None
    document_token: str | None = None
    registry: dict[str, int] = field(default_factory=dict)
    reverse_registry: dict[int, str] = field(default_factory=dict)
    element_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_element: int = 1
    acts: dict[str, tuple[str, dict[str, Any] | None]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    browser_closed: bool = False
    server_released: bool = False
    dev_server_stopped: bool = False


@dataclass(frozen=True, slots=True)
class _StoppedSession:
    project_id: str
    grant_id: str
    link_id: str
    connection_epoch: int
    dev_server_stopped: bool


@dataclass(slots=True)
class _PartialStartCleanup:
    project_id: str
    page: ManagedPage | None
    server: DevServerHandle | None
    browser_closed: bool = False
    server_released: bool = False
    task: asyncio.Task[None] | None = None


class FrontendSessionManager:
    """Own frontend authority, element generations, browser and server lifecycles."""

    def __init__(
        self,
        database: AgentDatabase,
        resolver: ProjectPathResolver,
        approvals: ApprovalManager,
        dev_servers: DevServerManager,
        browser: BrowserBackend,
        data_directory: Path,
        *,
        account_id: str,
        device_id: str,
    ) -> None:
        self.database = database
        self.resolver = resolver
        self.approvals = approvals
        self.dev_servers = dev_servers
        self.browser = browser
        self.data_directory = data_directory
        self.account_id = account_id
        self.device_id = device_id
        self._sessions: dict[str, FrontendSession] = {}
        self._project_sessions: dict[str, str] = {}
        self._starts: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        self._start_tasks: dict[
            tuple[str, str, str, str], tuple[str, asyncio.Task[ToolResponse]]
        ] = {}
        self._project_starts: dict[str, tuple[str, str, str, str]] = {}
        self._stopped: dict[str, _StoppedSession] = {}
        self._closing: dict[str, tuple[FrontendSession, asyncio.Task[bool]]] = {}
        self._partial_cleanups: dict[str, _PartialStartCleanup] = {}
        self._guard = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        self._lifecycle_generation = 0
        self._shutdown = False

    def start(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop(), name="codito-frontend-reaper")

    async def execute(
        self,
        request: Any,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        operation = str(request.operation)
        if operation == "session_start":
            return await self._start_session(
                request,
                grant_id=grant_id,
                link_id=link_id,
                connection_epoch=connection_epoch,
                deadline_at=deadline_at,
            )
        if operation == "session_stop":
            return await self._stop_request(
                request,
                grant_id=grant_id,
                link_id=link_id,
                connection_epoch=connection_epoch,
                deadline_at=deadline_at,
            )
        session = await self._get_session(
            request.project_id,
            request.session_id,
            grant_id=grant_id,
            link_id=link_id,
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
        )
        async with session.lock:
            self._ensure_session(
                session,
                request.project_id,
                grant_id=grant_id,
                link_id=link_id,
                connection_epoch=connection_epoch,
                deadline_at=deadline_at,
            )
            if operation == "snapshot":
                response = await self._snapshot(session, int(request.max_elements))
            elif operation == "inspect":
                response = await self._inspect(session, request)
            elif operation == "act":
                response = await self._act(session, request)
            elif operation == "source":
                response = await self._source(session, request)
            else:
                raise AgentError("invalid_request", "Unsupported project_frontend operation")
            # An action has crossed an external side-effect boundary. Once the
            # browser adapter confirms completion and journals the idempotency
            # result, a security/deadline flip must not rewrite that success as
            # a definitive failure that encourages a duplicate action.
            if operation != "act":
                self._ensure_session(
                    session,
                    request.project_id,
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
            return response

    async def _start_session(
        self,
        request: Any,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        request_value = request.model_dump(mode="json")
        route_parts = urlsplit(request.route)
        if route_parts.query or route_parts.fragment:
            raise AgentError(
                "invalid_request",
                "Frontend route queries and fragments are not accepted; navigate through the UI",
            )
        digest = action_digest(request_value)
        start_key = (grant_id, link_id, request.project_id, request.idempotency_key)
        async with self._guard:
            if self._shutdown:
                raise AgentError("frontend_session_closed", "Frontend manager is shutting down")
            prior = self._starts.get(start_key)
            if prior is not None:
                if prior[0] != digest:
                    raise AgentError(
                        "idempotency_conflict", "Frontend start idempotency key was reused"
                    )
                existing = self._sessions.get(prior[1])
                if existing is not None:
                    previous_epoch = existing.connection_epoch
                    self._ensure_session(
                        existing,
                        request.project_id,
                        grant_id=grant_id,
                        link_id=link_id,
                        connection_epoch=connection_epoch,
                        deadline_at=deadline_at,
                    )
                    return self._start_response(
                        existing, recovered=connection_epoch > previous_epoch
                    )
                raise AgentError(
                    "outcome_unknown",
                    "The frontend session may have started but is no longer recoverable",
                )
            pending = self._start_tasks.get(start_key)
            if pending is not None:
                if pending[0] != digest:
                    raise AgentError(
                        "idempotency_conflict", "Frontend start idempotency key was reused"
                    )
                task = pending[1]
            elif (
                (session_id := self._project_sessions.get(request.project_id)) is not None
                and (existing := self._sessions.get(session_id)) is not None
                and self._stable_binding_matches(
                    existing,
                    request.project_id,
                    grant_id=grant_id,
                    link_id=link_id,
                )
                and connection_epoch > existing.connection_epoch
            ):
                self._ensure_session(
                    existing,
                    request.project_id,
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
                self._starts[start_key] = (digest, existing.session_id)
                return self._start_response(existing, recovered=True)
            elif (
                request.project_id in self._project_sessions
                or request.project_id in self._project_starts
                or request.project_id in self._partial_cleanups
            ):
                raise AgentError(
                    "frontend_session_exists",
                    "A frontend session already exists or is starting for this project",
                )
            elif (
                len(self._sessions)
                + len(self._closing)
                + len(self._project_starts)
                + len(self._partial_cleanups)
                >= MAX_SESSIONS
            ):
                raise AgentError(
                    "too_many_sessions", "The managed frontend session limit was reached"
                )
            else:
                lifecycle_generation = self._lifecycle_generation
                task = asyncio.create_task(
                    self._run_start(
                        request,
                        start_key=start_key,
                        digest=digest,
                        grant_id=grant_id,
                        link_id=link_id,
                        connection_epoch=connection_epoch,
                        deadline_at=deadline_at,
                        lifecycle_generation=lifecycle_generation,
                    ),
                    name=f"codito-frontend-start-{request.project_id}",
                )
                self._start_tasks[start_key] = (digest, task)
                self._project_starts[request.project_id] = start_key
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
            raise

    async def _run_start(
        self,
        request: Any,
        *,
        start_key: tuple[str, str, str, str],
        digest: str,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
        lifecycle_generation: int,
    ) -> ToolResponse:
        server: DevServerHandle | None = None
        page: ManagedPage | None = None
        try:
            self._ensure_lifecycle(lifecycle_generation)
            project = self.database.get_project(request.project_id)
            self._validate_project(project)
            config = self.dev_servers.discover(project)
            generation = self.approvals.generation
            approval_deadline = deadline_at
            if project.mode is not ProjectMode.FULL_ACCESS:
                approval_deadline = deadline_at - (
                    timedelta(seconds=int(request.ready_timeout_seconds)) + FRONTEND_BROWSER_RESERVE
                )
                if approval_deadline <= datetime.now(UTC) + MIN_FRONTEND_APPROVAL_WINDOW:
                    raise AgentError(
                        "deadline_exceeded",
                        "The frontend start deadline cannot reserve enough time for approval, "
                        "server readiness, and browser startup",
                    )
            approval = ApprovalManager.build_request(
                account_id=self.account_id,
                grant_id=grant_id,
                link_id=link_id,
                device_id=self.device_id,
                project_id=project.project_id,
                project_title=project.title,
                capability="frontend:control",
                action_digest=action_digest(
                    {
                        "project_id": project.project_id,
                        "root_fingerprint": project.root_fingerprint,
                        "mode": project.mode.value,
                        "command": config.command,
                        "working_directory": config.working_relative,
                        "base_url": config.base_url,
                        "route": request.route,
                        "viewport": request.viewport.model_dump(mode="json"),
                    }
                ),
                connection_epoch=connection_epoch,
                deadline_at=approval_deadline,
                risk=ApprovalRisk.NATIVE_EXECUTION,
                summary=(
                    f"Frontend origin: {config.base_url}\n"
                    f"Viewport: {request.viewport.width} x {request.viewport.height}\n"
                    f"Route: {request.route}\n"
                    f"Purpose: {' '.join(request.purpose.split())}\n"
                    "Start a project dev server if needed and control a Codito-owned browser.\n"
                    "Popups, downloads, password "
                    "entry, and navigation away from this loopback origin are blocked."
                ),
                command={"kind": "script", "shell": "powershell", "script": config.command},
                working_directory=str(config.working_directory),
                environment_differences={},
                requested_network=True,
            )
            if project.mode is not ProjectMode.FULL_ACCESS:
                await self.approvals.request(approval, session_eligible=False)
            self._ensure_lifecycle(lifecycle_generation)
            self.approvals.ensure_current(generation, deadline_at)
            current = self.database.get_project(project.project_id)
            self._same_project(project, current)
            if self.dev_servers.discover(current) != config:
                raise AgentError(
                    "approval_expired", "Frontend configuration changed after local approval"
                )
            server = await self.dev_servers.acquire(
                project,
                config,
                ready_timeout_seconds=int(request.ready_timeout_seconds),
            )
            self._ensure_lifecycle(lifecycle_generation)
            self.approvals.ensure_current(generation, deadline_at)
            self._same_project(project, self.database.get_project(project.project_id))
            full_url = config.base_url.rstrip("/") + request.route
            public_url = _strip_url_query(full_url)
            page = await self.browser.open(
                profile_directory=self.data_directory
                / "browser-profiles"
                / _profile_key(project.project_id, project.root_fingerprint),
                url=full_url,
                allowed_origin=config.base_url,
                viewport=(int(request.viewport.width), int(request.viewport.height)),
            )
            current_url = getattr(self.browser, "current_url", None)
            if callable(current_url):
                public_url = _strip_url_query(str(current_url(page)))
            if _page_origin(public_url) != config.base_url:
                raise AgentError("navigation_blocked", "The page left its approved loopback origin")
            self._ensure_lifecycle(lifecycle_generation)
            self.approvals.ensure_current(generation, deadline_at)
            self._same_project(project, self.database.get_project(project.project_id))
            session = FrontendSession(
                session_id=f"frontend_{secrets.token_urlsafe(24)}",
                project_id=project.project_id,
                project_root=project.root,
                root_fingerprint=project.root_fingerprint,
                project_mode=project.mode,
                grant_id=grant_id,
                link_id=link_id,
                connection_epoch=connection_epoch,
                security_generation=generation,
                config=config,
                server=server,
                browser=page,
                viewport=(int(request.viewport.width), int(request.viewport.height)),
                public_url=public_url,
            )
            async with self._guard:
                self._ensure_lifecycle(lifecycle_generation)
                if self._project_starts.get(project.project_id) != start_key:
                    raise AgentError("approval_expired", "Frontend start reservation changed")
                self._sessions[session.session_id] = session
                self._project_sessions[project.project_id] = session.session_id
                self._starts[start_key] = (digest, session.session_id)
            server = None
            page = None
            return self._start_response(session)
        except BaseException:
            if page is not None or server is not None:
                cleanup_state = _PartialStartCleanup(request.project_id, page, server)
                self._partial_cleanups[request.project_id] = cleanup_state
                cleanup = self._start_partial_cleanup(cleanup_state)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(cleanup)
            raise
        finally:
            # No await here: cancellation after session commit must not strand the
            # reservation. Event-loop dictionary updates are atomic between awaits.
            start_record = self._start_tasks.get(start_key)
            if start_record is not None and start_record[1] is asyncio.current_task():
                self._start_tasks.pop(start_key, None)
            if self._project_starts.get(request.project_id) == start_key:
                self._project_starts.pop(request.project_id, None)

    def _start_partial_cleanup(self, state: _PartialStartCleanup) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._finish_partial_cleanup(state),
            name=f"codito-frontend-partial-cleanup-{state.project_id}",
        )
        state.task = task
        return task

    async def _finish_partial_cleanup(self, state: _PartialStartCleanup) -> None:
        failure: Exception | None = None
        if state.page is not None and not state.browser_closed:
            try:
                await self.browser.close_page(state.page)
                state.browser_closed = True
            except Exception as exc:
                failure = exc
        if state.server is not None and not state.server_released:
            try:
                await self.dev_servers.release(state.server)
                state.server_released = True
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure
        async with self._guard:
            if self._partial_cleanups.get(state.project_id) is state:
                self._partial_cleanups.pop(state.project_id, None)

    def _ensure_lifecycle(self, generation: int) -> None:
        if self._shutdown or generation != self._lifecycle_generation:
            raise AgentError("approval_expired", "Frontend lifecycle authority changed")

    def _start_response(self, session: FrontendSession, *, recovered: bool = False) -> ToolResponse:
        warnings = []
        if recovered:
            warnings.append(
                "Recovered the existing frontend session after transport reconnect; "
                "capture a fresh snapshot before continuing."
            )
        if not session.server.owned:
            warnings.append(
                "Reused the explicitly configured loopback server; Codito will not stop it."
            )
        result = {
            "operation": "session_start",
            "project_id": session.project_id,
            "session_id": session.session_id,
            "base_url": session.config.base_url,
            "url": session.public_url,
            "viewport": {"width": session.viewport[0], "height": session.viewport[1]},
            "dev_server": {
                "ownership": "managed" if session.server.owned else "reused",
                "health": "ready",
            },
            "warnings": warnings,
        }
        return ToolResponse(result, "Managed frontend session is ready.")

    async def _get_session(
        self,
        project_id: str,
        session_id: str,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> FrontendSession:
        async with self._guard:
            session = self._sessions.get(session_id)
        if session is None:
            raise AgentError("frontend_session_not_found", "The frontend session is unavailable")
        self._ensure_session(
            session,
            project_id,
            grant_id=grant_id,
            link_id=link_id,
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
        )
        return session

    def _ensure_session(
        self,
        session: FrontendSession,
        project_id: str,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> None:
        now = datetime.now(UTC)
        if session.closed:
            raise AgentError("frontend_session_closed", "The frontend session is closed")
        if now - session.created_at > SESSION_MAX or now - session.last_used_at > SESSION_IDLE:
            raise AgentError("frontend_session_expired", "The frontend session expired")
        if not self._stable_binding_matches(
            session,
            project_id,
            grant_id=grant_id,
            link_id=link_id,
        ):
            raise AgentError("binding_mismatch", "Frontend session binding does not match")
        if connection_epoch < session.connection_epoch:
            raise AgentError(
                "stale_connection", "Frontend request belongs to a stale connection epoch"
            )
        self.approvals.ensure_current(session.security_generation, deadline_at)
        project = self.database.get_project(session.project_id)
        if (
            not project.enabled
            or project.root != session.project_root
            or project.root_fingerprint != session.root_fingerprint
            or project.mode is not session.project_mode
        ):
            raise AgentError("approval_expired", "Frontend project access changed")
        self.resolver.verify_project(project)
        # The epoch fences stale transport work but is not part of the durable
        # account/device/project/grant ownership of a managed browser session.
        session.connection_epoch = connection_epoch
        session.last_used_at = now

    @staticmethod
    def _stable_binding_matches(
        session: FrontendSession,
        project_id: str,
        *,
        grant_id: str,
        link_id: str,
    ) -> bool:
        return (
            session.project_id == project_id
            and session.grant_id == grant_id
            and session.link_id == link_id
        )

    @staticmethod
    def _validate_project(project: Project) -> None:
        if not project.enabled:
            raise AgentError("project_disabled", "The requested project is disabled")
        if project.mode is ProjectMode.ISOLATED:
            raise AgentError(
                "sandbox_unavailable", "Managed frontend sessions require a native project mode"
            )

    @staticmethod
    def _same_project(expected: Project, current: Project) -> None:
        if (
            not current.enabled
            or current.root != expected.root
            or current.root_fingerprint != expected.root_fingerprint
            or current.mode is not expected.mode
        ):
            raise AgentError("approval_expired", "Frontend project access changed")

    async def _snapshot(self, session: FrontendSession, max_elements: int) -> ToolResponse:
        capture = await self.browser.snapshot(session.browser, max_elements=max_elements)
        snapshot_id = f"snapshot_{secrets.token_urlsafe(24)}"
        session.snapshot_id = snapshot_id
        session.document_token = capture.document_token
        session.registry.clear()
        session.reverse_registry.clear()
        session.element_cache.clear()
        session.next_element = 1
        for item in capture.elements:
            backend = item.get("backend_node_id")
            if isinstance(backend, int) and backend not in session.reverse_registry:
                assigned_id = f"e{session.next_element}"
                session.next_element += 1
                session.registry[assigned_id] = backend
                session.reverse_registry[backend] = assigned_id
        elements: list[dict[str, Any]] = []
        for item in capture.elements:
            backend = item.get("backend_node_id")
            if not isinstance(backend, int):
                continue
            registered_id = session.reverse_registry.get(backend)
            if registered_id is None:
                continue
            parent_backend = item.get("parent_backend_node_id")
            parent_id = (
                session.reverse_registry.get(parent_backend)
                if isinstance(parent_backend, int)
                else None
            )
            element = self._element_from_raw(
                registered_id,
                item,
                parent_id=parent_id,
            )
            session.element_cache[registered_id] = element
            elements.append(element)
        console = []
        levels = {
            "debug": "debug",
            "info": "info",
            "log": "info",
            "warn": "warning",
            "warning": "warning",
            "error": "error",
        }
        for entry in capture.console:
            text = str(entry.get("text", ""))[:4_096]
            if text:
                console.append(
                    {"level": levels.get(str(entry.get("level", "")), "info"), "text": text}
                )
        network = []
        allowed_methods = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
        for entry in capture.network:
            status = entry.get("status")
            if entry.get("ok") or (isinstance(status, int) and status < 400):
                continue
            method = str(entry.get("method", "GET")).upper()
            if method not in allowed_methods:
                method = "GET"
            issue: dict[str, Any] = {"method": method, "url": str(entry.get("url", ""))[:2_048]}
            if isinstance(status, int) and 100 <= status <= 599:
                issue["status"] = status
            failure = entry.get("error")
            if failure:
                issue["failure"] = str(failure)[:1_024]
            network.append(issue)
        warnings = list(capture.warnings[:15])
        if capture.truncated:
            warnings.append("Element list was truncated.")
        result = {
            "operation": "snapshot",
            "project_id": session.project_id,
            "session_id": session.session_id,
            "snapshot_id": snapshot_id,
            "mime_type": "image/png",
            "width": capture.width,
            "height": capture.height,
            "captured_at": datetime.now(UTC).isoformat(),
            "sha256": hashlib.sha256(capture.png).hexdigest(),
            "image_base64": base64.b64encode(capture.png).decode("ascii"),
            "url": _strip_url_query(capture.url),
            "title": capture.title,
            "viewport": {"width": session.viewport[0], "height": session.viewport[1]},
            "elements": elements,
            "console": console[-100:],
            "network": network[-100:],
            "truncated": capture.truncated,
            "warnings": warnings,
        }
        return ToolResponse(
            result, f"Captured managed page with {len(elements)} semantic elements."
        )

    def _element_from_raw(
        self, element_id: str, item: dict[str, Any], *, parent_id: str | None
    ) -> dict[str, Any]:
        attrs = _as_dict(item.get("attributes"))
        ax = _as_dict(item.get("accessibility"))
        classes = [part[:256] for part in str(attrs.get("class", "")).split() if part][:64]
        box = _as_dict(item.get("box"))
        visible = item.get("visible")
        if not isinstance(visible, bool):
            visible = float(box.get("width", 0)) > 0 and float(box.get("height", 0)) > 0
        return {
            "element_id": element_id,
            "parent_id": parent_id,
            "tag": str(item.get("tag", "div"))[:64] or "div",
            "role": str(ax.get("role"))[:128] if ax.get("role") else None,
            "name": str(ax.get("name"))[:1_024] if ax.get("name") else None,
            "text": str(item.get("text"))[:16_384] if item.get("text") else None,
            "box": {
                "x": float(box.get("x", 0)),
                "y": float(box.get("y", 0)),
                "width": max(0.0, float(box.get("width", 0))),
                "height": max(0.0, float(box.get("height", 0))),
            },
            "visible": visible,
            "enabled": not bool(ax.get("disabled", False)),
            "focused": bool(ax.get("focused", False)),
            "classes": classes,
        }

    def _require_snapshot(self, session: FrontendSession, snapshot_id: str) -> None:
        if session.snapshot_id != snapshot_id:
            raise AgentError("stale_snapshot", "Capture a fresh snapshot before using this element")
        token_reader = getattr(self.browser, "document_token", None)
        current_token = (
            str(token_reader(session.browser))
            if callable(token_reader)
            else f"d{getattr(session.browser, 'document_counter', -1)}"
        )
        if session.document_token != current_token:
            session.snapshot_id = None
            session.registry.clear()
            session.reverse_registry.clear()
            session.element_cache.clear()
            raise AgentError("stale_snapshot", "The page navigated; capture a fresh snapshot")

    async def _inspect(self, session: FrontendSession, request: Any) -> ToolResponse:
        self._require_snapshot(session, request.snapshot_id)
        backend: int | None = None
        element_id: str | None = request.element_id
        if element_id is not None:
            backend = session.registry.get(element_id)
            if backend is None:
                raise AgentError("element_not_found", "Element is not in this snapshot")
        elif (
            request.x is None
            or request.y is None
            or request.x >= session.viewport[0]
            or request.y >= session.viewport[1]
        ):
            raise AgentError("invalid_request", "Inspect coordinates are outside the viewport")
        raw = await self.browser.inspect(
            session.browser,
            backend_node_id=backend,
            x=request.x,
            y=request.y,
        )
        self._require_snapshot(session, request.snapshot_id)
        raw_backend = raw.get("backend_node_id")
        if not isinstance(raw_backend, int):
            raise AgentError("element_not_found", "No inspectable element was found")
        backend = raw_backend
        if element_id is None:
            element_id = session.reverse_registry.get(backend)
            if element_id is None:
                element_id = f"e{session.next_element}"
                session.next_element += 1
                session.registry[element_id] = backend
                session.reverse_registry[backend] = element_id
        element = self._element_from_raw(element_id, raw, parent_id=None)
        session.element_cache[element_id] = element
        computed = [
            {"name": str(name)[:128], "value": str(value)[:2_048]}
            for name, value in (raw.get("computed") or {}).items()
            if name and value is not None
        ][:128]
        matched_rules = []
        for rule in raw.get("matched_rules", [])[:128]:
            declarations = [
                {"name": str(name)[:128], "value": str(value)[:2_048]}
                for name, value in (rule.get("declarations") or {}).items()
                if name and value is not None
            ][:128]
            selector = str(rule.get("selector", ""))[:2_048]
            if selector and declarations:
                matched_rules.append({"selector": selector, "declarations": declarations})
        ax = _as_dict(raw.get("accessibility"))
        result = {
            "operation": "inspect",
            "project_id": session.project_id,
            "session_id": session.session_id,
            "snapshot_id": request.snapshot_id,
            "element": element,
            "computed_styles": computed,
            "matched_rules": matched_rules,
            "accessibility": {
                "role": str(ax.get("role"))[:128] if ax.get("role") else None,
                "name": str(ax.get("name"))[:1_024] if ax.get("name") else None,
                "description": str(ax.get("description"))[:2_048]
                if ax.get("description")
                else None,
                "disabled": ax.get("disabled") if isinstance(ax.get("disabled"), bool) else None,
                "expanded": ax.get("expanded") if isinstance(ax.get("expanded"), bool) else None,
                "checked": ax.get("checked")
                if ax.get("checked") in {True, False, "mixed"}
                else None,
            },
            "warnings": [],
        }
        return ToolResponse(result, f"Inspected {element_id} from the current page snapshot.")

    async def _act(self, session: FrontendSession, request: Any) -> ToolResponse:
        value = request.model_dump(mode="json")
        digest = action_digest(value)
        prior = session.acts.get(request.idempotency_key)
        if prior is not None:
            if prior[0] != digest:
                raise AgentError("idempotency_conflict", "Frontend action key was reused")
            if prior[1] is None:
                raise AgentError(
                    "outcome_unknown",
                    "The prior frontend action outcome is unknown; do not replay it",
                )
            return ToolResponse(prior[1], "Frontend action was already completed.")
        self._require_snapshot(session, request.snapshot_id)
        backend = session.registry.get(request.element_id)
        if backend is None:
            raise AgentError("element_not_found", "Element is not in this snapshot")
        try:
            await self.browser.act(
                session.browser,
                backend_node_id=backend,
                action=request.action.value,
                text=request.text,
                key=request.key.value if request.key is not None else None,
                option=request.option,
                delta_x=float(request.delta_x or 0),
                delta_y=float(request.delta_y or 0),
                expected_document_token=session.document_token or "",
            )
        except AgentError as exc:
            if exc.code == "outcome_unknown":
                session.acts[request.idempotency_key] = (digest, None)
                self._bound_acts(session)
                self._invalidate_snapshot(session)
            raise
        result = {
            "operation": "act",
            "project_id": session.project_id,
            "session_id": session.session_id,
            "snapshot_id": request.snapshot_id,
            "element_id": request.element_id,
            "action": request.action.value,
            "status": "completed",
            "snapshot_invalidated": True,
            "warnings": [],
        }
        session.acts[request.idempotency_key] = (digest, result)
        self._bound_acts(session)
        self._invalidate_snapshot(session)
        return ToolResponse(result, "Frontend action completed; capture a fresh snapshot.")

    @staticmethod
    def _bound_acts(session: FrontendSession) -> None:
        if len(session.acts) > 128:
            session.acts.pop(next(iter(session.acts)))

    @staticmethod
    def _invalidate_snapshot(session: FrontendSession) -> None:
        session.snapshot_id = None
        session.registry.clear()
        session.reverse_registry.clear()
        session.element_cache.clear()

    async def _source(self, session: FrontendSession, request: Any) -> ToolResponse:
        self._require_snapshot(session, request.snapshot_id)
        backend = session.registry.get(request.element_id)
        if backend is None:
            raise AgentError("element_not_found", "Element is not in this snapshot")
        attrs = await self.browser.source_attributes(session.browser, backend)
        self._require_snapshot(session, request.snapshot_id)
        instrumented_consumer = self._validated_location(session, attrs.get("data-codito-source"))
        host = self._validated_location(session, attrs.get("data-codito-host-source"))
        warnings: list[str] = []
        if attrs.get("data-codito-source") and instrumented_consumer is None:
            warnings.append("Invalid consumer source metadata was ignored.")
        if attrs.get("data-codito-host-source") and host is None:
            warnings.append("Invalid host source metadata was ignored.")
        design_system: list[dict[str, Any]] = []
        consumer: dict[str, Any] | None
        if instrumented_consumer is not None:
            consumer = instrumented_consumer
            confidence = "exact" if session.server.owned else "heuristic"
        else:
            consumer = host
            confidence = "heuristic" if host is not None else "unavailable"
        if instrumented_consumer is not None and host is not None and host != consumer:
            design_system.append({"location": host})
        if consumer is not None and not session.server.owned:
            warnings.append("Source metadata from a reused server cannot be process-bound.")
        result = {
            "operation": "source",
            "project_id": session.project_id,
            "session_id": session.session_id,
            "snapshot_id": request.snapshot_id,
            "element_id": request.element_id,
            "confidence": confidence,
            "consumer": {"location": consumer} if consumer is not None else None,
            "design_system": design_system,
            "styles": [],
            "warnings": warnings,
        }
        return ToolResponse(
            result,
            f"Resolved {confidence} source instrumentation."
            if confidence != "unavailable"
            else "No validated source instrumentation is available.",
        )

    def _validated_location(
        self, session: FrontendSession, raw: str | None
    ) -> dict[str, Any] | None:
        if not isinstance(raw, str) or len(raw) > 2_048:
            return None
        try:
            relative, line_raw, character_raw = raw.rsplit(":", 2)
            relative = validate_relative_path(relative)
            line = int(line_raw)
            character = int(character_raw)
            if not relative or not 1 <= line <= 10_000_000 or not 1 <= character <= 100_000:
                return None
            project = self.database.get_project(session.project_id)
            validated = self.resolver.resolve(project, relative, directory=False)
            with self.resolver.open_read(project, validated.relative) as stream:
                source = stream.read(MAX_SOURCE_BYTES + 1)
            if len(source) > MAX_SOURCE_BYTES or b"\x00" in source:
                return None
            lines = source.decode("utf-8").splitlines()
            if line > len(lines) or character > len(lines[line - 1]) + 1:
                return None
            return {"path": validated.relative, "line": line, "character": character}
        except (AgentError, UnicodeDecodeError, ValueError):
            return None

    async def _stop_request(
        self,
        request: Any,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        status = "already_stopped"
        async with self._guard:
            session = self._sessions.get(request.session_id)
            closing = self._closing.get(request.session_id)
            tombstone = self._stopped.get(request.session_id)
            if session is not None:
                self._validate_stop_binding(
                    session,
                    project_id=request.project_id,
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
                close_task = self._begin_close_locked(session)
                status = "stopped"
            elif closing is not None:
                self._validate_stop_binding(
                    closing[0],
                    project_id=request.project_id,
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
                close_task = self._begin_close_locked(closing[0])
                status = "stopped"
            else:
                if (
                    tombstone is None
                    or tombstone.project_id != request.project_id
                    or tombstone.grant_id != grant_id
                    or tombstone.link_id != link_id
                ):
                    raise AgentError(
                        "frontend_session_not_found", "The frontend session is unavailable"
                    )
                self._ensure_stop_epoch(
                    previous_epoch=tombstone.connection_epoch,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
                if connection_epoch > tombstone.connection_epoch:
                    tombstone = _StoppedSession(
                        tombstone.project_id,
                        tombstone.grant_id,
                        tombstone.link_id,
                        connection_epoch,
                        tombstone.dev_server_stopped,
                    )
                    self._stopped[request.session_id] = tombstone
                result = {
                    "operation": "session_stop",
                    "project_id": request.project_id,
                    "session_id": request.session_id,
                    "status": "already_stopped",
                    "dev_server_stopped": tombstone.dev_server_stopped,
                    "warnings": [],
                }
                return ToolResponse(result, "Frontend session was already stopped.")
        stopped = await asyncio.shield(close_task)
        result = {
            "operation": "session_stop",
            "project_id": request.project_id,
            "session_id": request.session_id,
            "status": status,
            "dev_server_stopped": stopped,
            "warnings": [],
        }
        return ToolResponse(result, f"Frontend session {status.replace('_', ' ')}.")

    def _validate_stop_binding(
        self,
        session: FrontendSession,
        *,
        project_id: str,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> None:
        if not self._stable_binding_matches(
            session,
            project_id,
            grant_id=grant_id,
            link_id=link_id,
        ):
            raise AgentError("binding_mismatch", "Frontend session binding does not match")
        self._ensure_stop_epoch(
            previous_epoch=session.connection_epoch,
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
        )
        session.connection_epoch = connection_epoch

    @staticmethod
    def _ensure_stop_epoch(
        *, previous_epoch: int, connection_epoch: int, deadline_at: datetime
    ) -> None:
        if deadline_at <= datetime.now(UTC):
            raise AgentError("deadline_exceeded", "Frontend stop deadline expired")
        if connection_epoch < previous_epoch:
            raise AgentError(
                "stale_connection", "Frontend stop belongs to a stale connection epoch"
            )

    def _begin_close_locked(self, session: FrontendSession) -> asyncio.Task[bool]:
        existing = self._closing.get(session.session_id)
        if existing is not None:
            existing_task = existing[1]
            if not existing_task.done():
                return existing_task
            if not existing_task.cancelled() and existing_task.exception() is None:
                return existing_task
        session.closed = True
        self._sessions.pop(session.session_id, None)
        self._starts = {
            key: value for key, value in self._starts.items() if value[1] != session.session_id
        }
        task = asyncio.create_task(
            self._finish_close(session), name=f"codito-frontend-close-{session.session_id}"
        )
        self._closing[session.session_id] = (session, task)
        return task

    async def _finish_close(self, session: FrontendSession) -> bool:
        failure: Exception | None = None
        async with session.lock:
            if not session.browser_closed:
                try:
                    await self.browser.close_page(session.browser)
                    session.browser_closed = True
                except AgentError as exc:
                    failure = exc
                except Exception:
                    failure = AgentError(
                        "browser_close_failed",
                        "The managed browser profile could not be closed",
                        retryable=True,
                    )
            if not session.server_released:
                try:
                    session.dev_server_stopped = await self.dev_servers.release(session.server)
                    session.server_released = True
                except Exception as exc:
                    failure = failure or exc
            if failure is not None:
                raise failure
        async with self._guard:
            self._stopped[session.session_id] = _StoppedSession(
                session.project_id,
                session.grant_id,
                session.link_id,
                session.connection_epoch,
                session.dev_server_stopped,
            )
            self._closing.pop(session.session_id, None)
            if self._project_sessions.get(session.project_id) == session.session_id:
                self._project_sessions.pop(session.project_id, None)
            while len(self._stopped) > TOMBSTONE_MAX:
                self._stopped.pop(next(iter(self._stopped)))
        return session.dev_server_stopped

    async def close_project(self, project_id: str) -> None:
        async with self._guard:
            session_id = self._project_sessions.get(project_id)
            session = self._sessions.get(session_id) if session_id else None
            closing = self._closing.get(session_id) if session_id else None
            task = (
                self._begin_close_locked(session)
                if session is not None
                else self._begin_close_locked(closing[0])
                if closing is not None
                else None
            )
        if task is not None:
            await asyncio.shield(task)

    async def close_invalid_sessions(self) -> None:
        tasks: list[asyncio.Task[bool]] = []
        async with self._guard:
            for session in list(self._sessions.values()):
                try:
                    project = self.database.get_project(session.project_id)
                    valid = (
                        session.security_generation == self.approvals.generation
                        and not session.closed
                        and project.enabled
                        and project.root == session.project_root
                        and project.root_fingerprint == session.root_fingerprint
                        and project.mode is session.project_mode
                    )
                except AgentError:
                    valid = False
                if not valid:
                    tasks.append(self._begin_close_locked(session))
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))

    async def disconnected(self) -> None:
        # A transport fence cancels work that has not established a session yet.
        # Established sessions retain their stable ownership and may rebind to the
        # next epoch. Long disconnects still clear the approval generation, and the
        # reaper/close_invalid_sessions then disposes those sessions.
        await self._cancel_pending_starts(shutdown=False)

    async def _cancel_pending_starts(self, *, shutdown: bool) -> None:
        async with self._guard:
            self._lifecycle_generation += 1
            self._shutdown = self._shutdown or shutdown
            tasks = [task for _, task in self._start_tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                now = datetime.now(UTC)
                tasks: list[asyncio.Task[Any]] = []
                async with self._guard:
                    expired: list[FrontendSession] = []
                    for session in self._sessions.values():
                        invalid = (
                            session.closed
                            or session.security_generation != self.approvals.generation
                        )
                        try:
                            project = self.database.get_project(session.project_id)
                            invalid = invalid or not (
                                project.enabled
                                and project.root == session.project_root
                                and project.root_fingerprint == session.root_fingerprint
                                and project.mode is session.project_mode
                            )
                        except AgentError:
                            invalid = True
                        if (
                            invalid
                            or now - session.created_at > SESSION_MAX
                            or now - session.last_used_at > SESSION_IDLE
                        ):
                            expired.append(session)
                    for session in expired:
                        tasks.append(self._begin_close_locked(session))
                    for session, closing_task in list(self._closing.values()):
                        if closing_task.done() and (
                            closing_task.cancelled() or closing_task.exception() is not None
                        ):
                            tasks.append(self._begin_close_locked(session))
                    for state in list(self._partial_cleanups.values()):
                        partial_task = state.task
                        if partial_task is None or (
                            partial_task.done()
                            and (partial_task.cancelled() or partial_task.exception() is not None)
                        ):
                            partial_task = self._start_partial_cleanup(state)
                        if not partial_task.done():
                            tasks.append(partial_task)
                if tasks:
                    await asyncio.gather(
                        *(asyncio.shield(task) for task in tasks), return_exceptions=True
                    )
        except asyncio.CancelledError:
            return

    async def _close_all_sessions(self) -> None:
        async with self._guard:
            tasks: list[asyncio.Task[Any]] = [
                self._begin_close_locked(session) for session in list(self._sessions.values())
            ]
            tasks.extend(
                self._begin_close_locked(session) for session, _task in list(self._closing.values())
            )
            for state in list(self._partial_cleanups.values()):
                task = state.task
                if task is None or (
                    task.done() and (task.cancelled() or task.exception() is not None)
                ):
                    task = self._start_partial_cleanup(state)
                tasks.append(task)
            tasks = list(dict.fromkeys(tasks))
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        await self._cancel_pending_starts(shutdown=True)
        await self._close_all_sessions()
        try:
            await self.dev_servers.close()
        finally:
            await self.browser.close()


def _strip_url_query(value: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _page_origin(value: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(value)
    host = parsed.hostname or ""
    port = parsed.port or 80
    if ":" in host:
        host = f"[{host}]"
    authority = host if port == 80 else f"{host}:{port}"
    return urlunsplit((parsed.scheme, authority, "", "", ""))


def _profile_key(project_id: str, root_fingerprint: str) -> str:
    identity = f"{project_id}\0{root_fingerprint}"
    return hashlib.sha256(identity.encode()).hexdigest()
