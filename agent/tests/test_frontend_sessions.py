from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from codito_protocol.frontend import validate_project_frontend, validate_project_frontend_result

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.db import AgentDatabase
from codito_agent.dev_servers import DevServerHandle, FrontendLaunchConfig
from codito_agent.errors import AgentError
from codito_agent.frontend_playwright import BrowserSnapshot
from codito_agent.frontend_sessions import FrontendSessionManager, _profile_key
from codito_agent.models import ProjectMode
from codito_agent.paths import ProjectPathResolver


def png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


class FakeDevServers:
    def __init__(self, root: Path, *, owned: bool = True) -> None:
        self.config = FrontendLaunchConfig(
            ".", root, "npm run dev", "http://localhost:5173", "test"
        )
        self.released = 0
        self.owned = owned

    def discover(self, _project):
        return self.config

    async def acquire(self, project, config, *, ready_timeout_seconds):
        assert config is self.config
        assert ready_timeout_seconds == 30
        return DevServerHandle("server", project.project_id, config, owned=self.owned)

    async def release(self, _handle):
        self.released += 1
        return True

    async def close(self):
        return None


class FakeBrowser:
    def __init__(self) -> None:
        self.page = SimpleNamespace(
            document_counter=0,
            mutation_counter=0,
            current_url="http://localhost:5173/",
        )
        self.redirect_url: str | None = None
        self.closed_pages = 0
        self.actions: list[dict[str, Any]] = []
        self.open_values: dict[str, Any] = {}
        self.mutate_on_inspect = False
        self.mutate_on_source = False
        self.mutate_on_act = False
        self.unknown_on_act = False
        self.source_values = {
            "data-codito-source": "src/App.tsx:1:2",
            "data-codito-host-source": "src/Button.tsx:1:1",
        }

    async def open(self, **kwargs):
        self.open_values = kwargs
        self.page.current_url = self.redirect_url or kwargs["url"]
        return self.page

    @staticmethod
    def current_url(record) -> str:
        return str(record.current_url)

    @staticmethod
    def document_token(record) -> str:
        return f"d{record.document_counter}:m{record.mutation_counter}"

    async def snapshot(self, _record, *, max_elements):
        assert max_elements == 500
        return BrowserSnapshot(
            png(320, 240),
            320,
            240,
            [
                {
                    "backend_node_id": 10,
                    "parent_backend_node_id": None,
                    "tag": "main",
                    "text": "",
                    "box": {"x": 0, "y": 0, "width": 320, "height": 240},
                    "attributes": {"class": "app"},
                    "computed": {},
                    "accessibility": {"role": "main", "name": "App"},
                },
                {
                    "backend_node_id": 11,
                    "parent_backend_node_id": 10,
                    "tag": "button",
                    "text": "Add",
                    "box": {"x": 10, "y": 10, "width": 80, "height": 32},
                    "attributes": {"class": "button primary"},
                    "computed": {},
                    "accessibility": {"role": "button", "name": "Add"},
                },
            ],
            [{"level": "log", "text": "ready"}],
            [
                {"method": "GET", "url": "http://localhost:5173/ok", "status": 200, "ok": True},
                {"method": "GET", "url": "http://localhost:5173/fail", "status": 500, "ok": False},
            ],
            False,
            self.document_token(self.page),
            "http://localhost:5173/dashboard?token=must-not-return#private",
            "Dashboard",
        )

    async def inspect(self, _record, *, backend_node_id=None, x=None, y=None):
        if self.mutate_on_inspect:
            self.page.mutation_counter += 1
        if backend_node_id is None:
            assert (x, y) == (20, 20)
            backend_node_id = 11
        return {
            "backend_node_id": backend_node_id,
            "tag": "button",
            "attributes": {"class": "button primary"},
            "box": {"x": 10, "y": 10, "width": 80, "height": 32},
            "computed": {"display": "inline-flex"},
            "matched_rules": [{"selector": ".button", "declarations": {"display": "inline-flex"}}],
            "accessibility": {"role": "button", "name": "Add"},
        }

    async def source_attributes(self, _record, backend_node_id):
        assert backend_node_id == 11
        if self.mutate_on_source:
            self.page.mutation_counter += 1
        return self.source_values

    async def act(self, _record, **values):
        if self.mutate_on_act:
            self.page.mutation_counter += 1
        if values["expected_document_token"] != self.document_token(self.page):
            raise AgentError("stale_snapshot", "The page changed before action dispatch")
        self.actions.append(values)
        if self.unknown_on_act:
            raise AgentError("outcome_unknown", "The browser acknowledgement was lost")

    async def close_page(self, _record):
        self.closed_pages += 1

    async def close(self):
        return None


async def allow_once(_request):
    return ApprovalDecision.ALLOW_ONCE


def request(value: dict[str, Any]):
    return validate_project_frontend(value)


def bindings(seconds: float = 300) -> dict[str, Any]:
    return {
        "grant_id": "grant_abcdefghijkl",
        "link_id": "link_abcdefghijklmn",
        "connection_epoch": 7,
        "deadline_at": datetime.now(UTC) + timedelta(seconds=seconds),
    }


@pytest.mark.asyncio
async def test_snapshot_refs_inspect_source_act_and_stop_are_generation_bound(
    tmp_path: Path, project_root: Path
) -> None:
    (project_root / "src").mkdir()
    (project_root / "src" / "App.tsx").write_text("export const App = 1\n", encoding="utf-8")
    (project_root / "src" / "Button.tsx").write_text("export const Button = 1\n", encoding="utf-8")
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    browser = FakeBrowser()
    servers = FakeDevServers(project_root)
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(allow_once),
        servers,
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    start = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review dashboard",
                "route": "/dashboard",
                "viewport": {"width": 320, "height": 240},
                "idempotency_key": "frontend_start_123456",
            }
        ),
        **bindings(),
    )
    start_result = validate_project_frontend_result(start.structured)
    assert start_result.url == "http://localhost:5173/dashboard"
    assert "token" not in start.text
    assert browser.open_values["profile_directory"].name == _profile_key(
        project.project_id, project.root_fingerprint
    )

    snapshot = await manager.execute(
        request(
            {
                "operation": "snapshot",
                "project_id": project.project_id,
                "session_id": start_result.session_id,
                "purpose": "See UI",
                "max_elements": 500,
            }
        ),
        **bindings(),
    )
    snapshot_result = validate_project_frontend_result(snapshot.structured)
    assert [item.element_id for item in snapshot_result.elements] == ["e1", "e2"]
    assert snapshot_result.elements[1].parent_id == "e1"
    assert snapshot_result.console[0].level == "info"
    assert len(snapshot_result.network) == 1
    assert snapshot_result.url == "http://localhost:5173/dashboard"

    inspected = await manager.execute(
        request(
            {
                "operation": "inspect",
                "project_id": project.project_id,
                "session_id": start_result.session_id,
                "purpose": "Inspect Add",
                "snapshot_id": snapshot_result.snapshot_id,
                "element_id": "e2",
            }
        ),
        **bindings(),
    )
    assert validate_project_frontend_result(inspected.structured).element.role == "button"

    source = await manager.execute(
        request(
            {
                "operation": "source",
                "project_id": project.project_id,
                "session_id": start_result.session_id,
                "purpose": "Find source",
                "snapshot_id": snapshot_result.snapshot_id,
                "element_id": "e2",
            }
        ),
        **bindings(),
    )
    source_result = validate_project_frontend_result(source.structured)
    assert source_result.confidence == "exact"
    assert source_result.consumer.location.path == "src/App.tsx"
    assert source_result.design_system[0].location.path == "src/Button.tsx"

    session = manager._sessions[start_result.session_id]
    session.server.owned = False
    reused = await manager.execute(
        request(
            {
                "operation": "source",
                "project_id": project.project_id,
                "session_id": start_result.session_id,
                "purpose": "Find reused source",
                "snapshot_id": snapshot_result.snapshot_id,
                "element_id": "e2",
            }
        ),
        **bindings(),
    )
    assert validate_project_frontend_result(reused.structured).confidence == "heuristic"

    browser.source_values = {"data-codito-host-source": "src/Button.tsx:1:1"}
    host_only = await manager.execute(
        request(
            {
                "operation": "source",
                "project_id": project.project_id,
                "session_id": start_result.session_id,
                "purpose": "Find host fallback",
                "snapshot_id": snapshot_result.snapshot_id,
                "element_id": "e2",
            }
        ),
        **bindings(),
    )
    host_result = validate_project_frontend_result(host_only.structured)
    assert host_result.confidence == "heuristic"
    assert host_result.consumer.location.path == "src/Button.tsx"
    assert host_result.design_system == []

    browser.source_values = {}
    unavailable = await manager.execute(
        request(
            {
                "operation": "source",
                "project_id": project.project_id,
                "session_id": start_result.session_id,
                "purpose": "Find unavailable source",
                "snapshot_id": snapshot_result.snapshot_id,
                "element_id": "e2",
            }
        ),
        **bindings(),
    )
    unavailable_result = validate_project_frontend_result(unavailable.structured)
    assert unavailable_result.confidence == "unavailable"
    assert unavailable_result.consumer is None

    await manager.execute(
        request(
            {
                "operation": "act",
                "project_id": project.project_id,
                "session_id": start_result.session_id,
                "purpose": "Click Add",
                "snapshot_id": snapshot_result.snapshot_id,
                "element_id": "e2",
                "action": "click",
                "idempotency_key": "frontend_click_12345",
            }
        ),
        **bindings(),
    )
    with pytest.raises(AgentError, match="fresh snapshot"):
        await manager.execute(
            request(
                {
                    "operation": "source",
                    "project_id": project.project_id,
                    "session_id": start_result.session_id,
                    "purpose": "Use stale source",
                    "snapshot_id": snapshot_result.snapshot_id,
                    "element_id": "e2",
                }
            ),
            **bindings(),
        )

    stopped = await manager.execute(
        request(
            {
                "operation": "session_stop",
                "project_id": project.project_id,
                "session_id": start_result.session_id,
                "purpose": "Done",
            }
        ),
        **bindings(),
    )
    assert validate_project_frontend_result(stopped.structured).status == "stopped"
    assert browser.closed_pages == servers.released == 1
    await manager.close()


@pytest.mark.asyncio
async def test_transient_reconnect_rebinds_and_recovers_frontend_session(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    browser = FakeBrowser()
    servers = FakeDevServers(project_root)
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(allow_once),
        servers,
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    start_request = request(
        {
            "operation": "session_start",
            "project_id": project.project_id,
            "purpose": "Review before reconnect",
            "viewport": {"width": 320, "height": 240},
            "idempotency_key": "frontend_start_reconnect",
        }
    )
    started = await manager.execute(start_request, **bindings())
    session_id = started.structured["session_id"]

    await manager.disconnected()
    assert session_id in manager._sessions
    assert browser.closed_pages == servers.released == 0

    recovered = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Recover after reconnect",
                "viewport": {"width": 320, "height": 240},
                "idempotency_key": "frontend_start_recovered",
            }
        ),
        **{**bindings(), "connection_epoch": 8},
    )
    assert recovered.structured["session_id"] == session_id
    assert "reconnect" in recovered.structured["warnings"][0].lower()

    snapshot = await manager.execute(
        request(
            {
                "operation": "snapshot",
                "project_id": project.project_id,
                "session_id": session_id,
                "purpose": "Continue after reconnect",
            }
        ),
        **{**bindings(), "connection_epoch": 8},
    )
    assert snapshot.structured["operation"] == "snapshot"
    assert manager._sessions[session_id].connection_epoch == 8

    with pytest.raises(AgentError) as stale:
        await manager.execute(
            request(
                {
                    "operation": "snapshot",
                    "project_id": project.project_id,
                    "session_id": session_id,
                    "purpose": "Reject stale transport work",
                }
            ),
            **bindings(),
        )
    assert stale.value.code == "stale_connection"

    stopped = await manager.execute(
        request(
            {
                "operation": "session_stop",
                "project_id": project.project_id,
                "session_id": session_id,
                "purpose": "Done after reconnect",
            }
        ),
        **{**bindings(), "connection_epoch": 8},
    )
    assert stopped.structured["status"] == "stopped"
    assert browser.closed_pages == servers.released == 1
    await manager.close()


@pytest.mark.asyncio
async def test_stop_can_cleanup_after_reconnect_and_authority_generation_change(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    approvals = ApprovalManager(allow_once)
    browser = FakeBrowser()
    servers = FakeDevServers(project_root)
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        approvals,
        servers,
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    started = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review",
                "idempotency_key": "frontend_start_cleanup",
            }
        ),
        **bindings(),
    )
    session_id = started.structured["session_id"]
    approvals.clear("long_disconnect")

    stopped = await manager.execute(
        request(
            {
                "operation": "session_stop",
                "project_id": project.project_id,
                "session_id": session_id,
                "purpose": "Cleanup stale authority",
            }
        ),
        **{**bindings(), "connection_epoch": 8},
    )
    assert stopped.structured["status"] == "stopped"
    assert browser.closed_pages == servers.released == 1

    repeated = await manager.execute(
        request(
            {
                "operation": "session_stop",
                "project_id": project.project_id,
                "session_id": session_id,
                "purpose": "Confirm cleanup",
            }
        ),
        **{**bindings(), "connection_epoch": 9},
    )
    assert repeated.structured["status"] == "already_stopped"

    with pytest.raises(AgentError) as stale_stop:
        await manager.execute(
            request(
                {
                    "operation": "session_stop",
                    "project_id": project.project_id,
                    "session_id": session_id,
                    "purpose": "Reject stale cleanup transport",
                }
            ),
            **{**bindings(), "connection_epoch": 8},
        )
    assert stale_stop.value.code == "stale_connection"
    await manager.close()


@pytest.mark.asyncio
async def test_reconnect_never_rebinds_or_stops_session_for_a_different_owner(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(allow_once),
        FakeDevServers(project_root),
        FakeBrowser(),
        tmp_path,
        account_id="account",
        device_id="device",
    )
    started = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review",
                "idempotency_key": "frontend_start_owner",
            }
        ),
        **bindings(),
    )
    session_id = started.structured["session_id"]
    other_owner = {
        **bindings(),
        "grant_id": "grant_other_owner",
        "connection_epoch": 8,
    }

    with pytest.raises(AgentError) as existing:
        await manager.execute(
            request(
                {
                    "operation": "session_start",
                    "project_id": project.project_id,
                    "purpose": "Do not recover another owner's session",
                    "idempotency_key": "frontend_start_other_owner",
                }
            ),
            **other_owner,
        )
    assert existing.value.code == "frontend_session_exists"

    with pytest.raises(AgentError) as stop:
        await manager.execute(
            request(
                {
                    "operation": "session_stop",
                    "project_id": project.project_id,
                    "session_id": session_id,
                    "purpose": "Do not stop another owner's session",
                }
            ),
            **other_owner,
        )
    assert stop.value.code == "binding_mismatch"
    assert session_id in manager._sessions
    await manager.close()


@pytest.mark.asyncio
async def test_security_generation_and_viewport_coordinates_are_enforced(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    approvals = ApprovalManager(allow_once)
    browser = FakeBrowser()
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        approvals,
        FakeDevServers(project_root),
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    start = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review",
                "viewport": {"width": 320, "height": 240},
                "idempotency_key": "frontend_start_abcdef",
            }
        ),
        **bindings(),
    )
    snapshot = await manager.execute(
        request(
            {
                "operation": "snapshot",
                "project_id": project.project_id,
                "session_id": start.structured["session_id"],
                "purpose": "See UI",
            }
        ),
        **bindings(),
    )
    with pytest.raises(AgentError, match="outside the viewport"):
        await manager.execute(
            request(
                {
                    "operation": "inspect",
                    "project_id": project.project_id,
                    "session_id": start.structured["session_id"],
                    "purpose": "Bad pixel",
                    "snapshot_id": snapshot.structured["snapshot_id"],
                    "x": 500,
                    "y": 20,
                }
            ),
            **bindings(),
        )
    browser.page.mutation_counter += 1
    with pytest.raises(AgentError, match="fresh snapshot"):
        await manager.execute(
            request(
                {
                    "operation": "inspect",
                    "project_id": project.project_id,
                    "session_id": start.structured["session_id"],
                    "purpose": "Stale HMR element",
                    "snapshot_id": snapshot.structured["snapshot_id"],
                    "element_id": "e2",
                }
            ),
            **bindings(),
        )
    approvals.clear("test")
    with pytest.raises(AgentError, match="permission"):
        await manager.execute(
            request(
                {
                    "operation": "snapshot",
                    "project_id": project.project_id,
                    "session_id": start.structured["session_id"],
                    "purpose": "Stale authority",
                }
            ),
            **bindings(),
        )
    await manager.close()


def test_profile_identity_changes_with_project_root_fingerprint() -> None:
    assert _profile_key("project", "root-a") != _profile_key("project", "root-b")


@pytest.mark.asyncio
async def test_start_reports_same_origin_redirect_without_query(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    browser = FakeBrowser()
    browser.redirect_url = "http://localhost:5173/login?return=%2Fdashboard#private"
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(allow_once),
        FakeDevServers(project_root),
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )

    started = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Follow the app redirect",
                "route": "/dashboard",
                "idempotency_key": "frontend_start_redirect",
            }
        ),
        **bindings(),
    )

    assert validate_project_frontend_result(started.structured).url == (
        "http://localhost:5173/login"
    )
    await manager.close()


@pytest.mark.asyncio
async def test_approval_summary_keeps_trusted_frontend_details_ahead_of_purpose(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    seen = []

    async def deny(approval):
        seen.append(approval)
        return ApprovalDecision.DENY

    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(deny),
        FakeDevServers(project_root),
        FakeBrowser(),
        tmp_path,
        account_id="account",
        device_id="device",
    )
    with pytest.raises(AgentError):
        await manager.execute(
            request(
                {
                    "operation": "session_start",
                    "project_id": project.project_id,
                    "purpose": "x" * 1_000,
                    "route": "/dashboard",
                    "viewport": {"width": 320, "height": 240},
                    "idempotency_key": "frontend_start_summary",
                }
            ),
            **bindings(),
        )
    visible_summary = seen[0].summary[:200]
    assert "Frontend origin: http://localhost:5173" in visible_summary
    assert "Viewport: 320 x 240" in visible_summary
    assert "Route: /dashboard" in visible_summary
    await manager.close()


@pytest.mark.asyncio
async def test_start_approval_deadline_reserves_server_and_browser_budget(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    seen = []

    async def deny(approval):
        seen.append(approval)
        return ApprovalDecision.DENY

    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(deny),
        FakeDevServers(project_root),
        FakeBrowser(),
        tmp_path,
        account_id="account",
        device_id="device",
    )
    overall = datetime.now(UTC) + timedelta(seconds=180)
    with pytest.raises(AgentError):
        await manager.execute(
            request(
                {
                    "operation": "session_start",
                    "project_id": project.project_id,
                    "purpose": "Review with a bounded approval window",
                    "ready_timeout_seconds": 30,
                    "idempotency_key": "frontend_start_deadline_budget",
                }
            ),
            **{**bindings(), "deadline_at": overall},
        )
    assert len(seen) == 1
    assert timedelta(seconds=54) < seen[0].deadline_at - datetime.now(UTC)
    assert seen[0].deadline_at <= overall - timedelta(seconds=125)

    seen.clear()
    with pytest.raises(AgentError, match="cannot reserve enough time") as error:
        await manager.execute(
            request(
                {
                    "operation": "session_start",
                    "project_id": project.project_id,
                    "purpose": "Deadline is too short",
                    "ready_timeout_seconds": 30,
                    "idempotency_key": "frontend_start_deadline_boundary",
                }
            ),
            **bindings(129),
        )
    assert error.value.code == "deadline_exceeded"
    assert seen == []
    await manager.close()


class BlockingCloseBrowser(FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.finish_close = asyncio.Event()

    async def close_page(self, _record):
        self.close_started.set()
        await self.finish_close.wait()
        self.closed_pages += 1


class FailsOnceCloseBrowser(FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.close_attempts = 0

    async def close_page(self, _record):
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise OSError("context still open")
        self.closed_pages += 1


class InvalidatingOpenBrowser(FailsOnceCloseBrowser):
    def __init__(self, approvals: ApprovalManager) -> None:
        super().__init__()
        self.approvals = approvals
        self.invalidate_after_open = True

    async def open(self, **kwargs):
        page = await super().open(**kwargs)
        if self.invalidate_after_open:
            self.approvals.clear("test_start_race")
        return page


@pytest.mark.asyncio
async def test_cancelled_stop_finishes_cleanup_and_retry_uses_tombstone(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    browser = BlockingCloseBrowser()
    servers = FakeDevServers(project_root)
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(allow_once),
        servers,
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    start = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review",
                "viewport": {"width": 320, "height": 240},
                "idempotency_key": "frontend_start_cancel",
            }
        ),
        **bindings(),
    )
    stop_request = request(
        {
            "operation": "session_stop",
            "project_id": project.project_id,
            "session_id": start.structured["session_id"],
            "purpose": "Done",
        }
    )
    stopping = asyncio.create_task(manager.execute(stop_request, **bindings()))
    await browser.close_started.wait()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    browser.finish_close.set()
    for _ in range(20):
        if start.structured["session_id"] in manager._stopped:
            break
        await asyncio.sleep(0)
    retried = await manager.execute(stop_request, **bindings())
    assert validate_project_frontend_result(retried.structured).status == "already_stopped"
    assert browser.closed_pages == servers.released == 1
    await manager.close()


@pytest.mark.asyncio
async def test_failed_browser_close_is_not_tombstoned_and_can_be_retried(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    browser = FailsOnceCloseBrowser()
    servers = FakeDevServers(project_root)
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(allow_once),
        servers,
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    start = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review",
                "idempotency_key": "frontend_start_close_fault",
            }
        ),
        **bindings(),
    )
    session_id = start.structured["session_id"]
    stop_request = request(
        {
            "operation": "session_stop",
            "project_id": project.project_id,
            "session_id": session_id,
            "purpose": "Done",
        }
    )
    with pytest.raises(AgentError, match="could not be closed"):
        await manager.execute(stop_request, **bindings())
    assert session_id in manager._closing
    assert session_id not in manager._stopped
    assert manager._project_sessions[project.project_id] == session_id

    stopped = await manager.execute(stop_request, **bindings())
    assert validate_project_frontend_result(stopped.structured).status == "stopped"
    assert browser.close_attempts == 2
    assert servers.released == 1
    await manager.close()


@pytest.mark.asyncio
async def test_mutation_barriers_reject_stale_inspect_source_and_action(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    browser = FakeBrowser()
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(allow_once),
        FakeDevServers(project_root),
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    start = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review",
                "idempotency_key": "frontend_start_mutation",
            }
        ),
        **bindings(),
    )
    session_id = start.structured["session_id"]

    async def fresh_snapshot():
        return await manager.execute(
            request(
                {
                    "operation": "snapshot",
                    "project_id": project.project_id,
                    "session_id": session_id,
                    "purpose": "Refresh",
                }
            ),
            **bindings(),
        )

    snapshot = await fresh_snapshot()
    browser.mutate_on_inspect = True
    with pytest.raises(AgentError, match="fresh snapshot"):
        await manager.execute(
            request(
                {
                    "operation": "inspect",
                    "project_id": project.project_id,
                    "session_id": session_id,
                    "purpose": "Inspect stale",
                    "snapshot_id": snapshot.structured["snapshot_id"],
                    "element_id": "e2",
                }
            ),
            **bindings(),
        )

    browser.mutate_on_inspect = False
    snapshot = await fresh_snapshot()
    browser.mutate_on_source = True
    with pytest.raises(AgentError, match="fresh snapshot"):
        await manager.execute(
            request(
                {
                    "operation": "source",
                    "project_id": project.project_id,
                    "session_id": session_id,
                    "purpose": "Source stale source",
                    "snapshot_id": snapshot.structured["snapshot_id"],
                    "element_id": "e2",
                }
            ),
            **bindings(),
        )

    browser.mutate_on_source = False
    snapshot = await fresh_snapshot()
    browser.mutate_on_act = True
    with pytest.raises(AgentError, match="changed before action"):
        await manager.execute(
            request(
                {
                    "operation": "act",
                    "project_id": project.project_id,
                    "session_id": session_id,
                    "purpose": "Do not act stale",
                    "snapshot_id": snapshot.structured["snapshot_id"],
                    "element_id": "e2",
                    "action": "click",
                    "idempotency_key": "frontend_act_mutation",
                }
            ),
            **bindings(),
        )
    assert browser.actions == []
    await manager.close()


@pytest.mark.asyncio
async def test_unknown_action_is_invalidated_and_never_replayed(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    browser = FakeBrowser()
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        ApprovalManager(allow_once),
        FakeDevServers(project_root),
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    started = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review",
                "idempotency_key": "frontend_start_unknown_action",
            }
        ),
        **bindings(),
    )
    session_id = started.structured["session_id"]
    snapshot = await manager.execute(
        request(
            {
                "operation": "snapshot",
                "project_id": project.project_id,
                "session_id": session_id,
                "purpose": "Snapshot",
            }
        ),
        **bindings(),
    )
    action = request(
        {
            "operation": "act",
            "project_id": project.project_id,
            "session_id": session_id,
            "purpose": "Click once",
            "snapshot_id": snapshot.structured["snapshot_id"],
            "element_id": "e2",
            "action": "click",
            "idempotency_key": "frontend_act_unknown_once",
        }
    )
    browser.unknown_on_act = True

    for _ in range(2):
        with pytest.raises(AgentError) as error:
            await manager.execute(action, **bindings())
        assert error.value.code == "outcome_unknown"
    assert len(browser.actions) == 1
    assert manager._sessions[session_id].snapshot_id is None
    await manager.close()


@pytest.mark.asyncio
async def test_completed_action_survives_post_dispatch_security_flip(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    approvals = ApprovalManager(allow_once)
    browser = FakeBrowser()
    original_act = browser.act

    async def act_then_invalidate(record, **values):
        await original_act(record, **values)
        approvals.clear("after_dispatch")

    browser.act = act_then_invalidate
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        approvals,
        FakeDevServers(project_root),
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    started = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Review",
                "idempotency_key": "frontend_start_security_flip",
            }
        ),
        **bindings(),
    )
    snapshot = await manager.execute(
        request(
            {
                "operation": "snapshot",
                "project_id": project.project_id,
                "session_id": started.structured["session_id"],
                "purpose": "Refresh",
            }
        ),
        **bindings(),
    )
    acted = await manager.execute(
        request(
            {
                "operation": "act",
                "project_id": project.project_id,
                "session_id": started.structured["session_id"],
                "purpose": "Click once",
                "snapshot_id": snapshot.structured["snapshot_id"],
                "element_id": "e2",
                "action": "click",
                "idempotency_key": "frontend_act_security_flip",
            }
        ),
        **bindings(),
    )
    assert acted.structured["status"] == "completed"
    assert len(browser.actions) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_failed_partial_start_cleanup_is_quarantined_and_retried(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.FULL_ACCESS)
    approvals = ApprovalManager(allow_once)
    browser = InvalidatingOpenBrowser(approvals)
    servers = FakeDevServers(project_root)
    manager = FrontendSessionManager(
        database,
        ProjectPathResolver(),
        approvals,
        servers,
        browser,
        tmp_path,
        account_id="account",
        device_id="device",
    )
    with pytest.raises(AgentError, match="permission"):
        await manager.execute(
            request(
                {
                    "operation": "session_start",
                    "project_id": project.project_id,
                    "purpose": "Race authority",
                    "idempotency_key": "frontend_start_partial_one",
                }
            ),
            **bindings(),
        )
    assert project.project_id in manager._partial_cleanups
    assert servers.released == 1
    with pytest.raises(AgentError, match="already exists or is starting"):
        await manager.execute(
            request(
                {
                    "operation": "session_start",
                    "project_id": project.project_id,
                    "purpose": "Blocked by cleanup",
                    "idempotency_key": "frontend_start_partial_two",
                }
            ),
            **bindings(),
        )

    await manager._close_all_sessions()
    assert project.project_id not in manager._partial_cleanups
    browser.invalidate_after_open = False
    start = await manager.execute(
        request(
            {
                "operation": "session_start",
                "project_id": project.project_id,
                "purpose": "Clean retry",
                "idempotency_key": "frontend_start_partial_three",
            }
        ),
        **bindings(),
    )
    assert start.structured["operation"] == "session_start"
    await manager.close()
