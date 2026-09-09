from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from codito_agent.config import AgentConfig
from codito_agent.read_tools import ToolResponse
from codito_agent.relay_client import RelayTokenState


def test_daemon_construction_wires_dynamic_link_without_stale_constructor_argument(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from codito_agent import daemon as daemon_module

    state = RelayTokenState(
        access_token="access",  # noqa: S106 - inert test value
        refresh_token="refresh",  # noqa: S106 - inert test value
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        link_id="link_abcdefghijklmnop",
        mcp_url="https://example.test/mcp/d/link_abcdefghijklmnop",
    )

    class TokenStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def load(self) -> RelayTokenState:
            return state

    class CredentialStore:
        def __init__(self, path: Path) -> None:
            self.path = path

    class SecretStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def load_or_create(self) -> bytes:
            return b"k" * 32

    class Pipe:
        def __init__(self, *args: object) -> None:
            self.args = args

    monkeypatch.setattr(daemon_module, "RelayTokenStore", TokenStore)
    monkeypatch.setattr(daemon_module, "DeviceCredentialStore", CredentialStore)
    monkeypatch.setattr(daemon_module, "IpcSecretStore", SecretStore)
    monkeypatch.setattr(daemon_module, "NamedPipeServer", Pipe)

    daemon = daemon_module.CoditoDaemon(AgentConfig(data_directory=tmp_path))
    assert daemon.shells.account_id == state.account_id
    assert daemon.adapter.device_id == state.device_id
    status = daemon._handle_ipc({"action": "status"})
    assert status["account_id"] == state.account_id
    assert status["device_id"] == state.device_id
    assert status["mcp_url"] == state.mcp_url
    assert status["connection_epoch"] == 0
    assert status["pending_approvals"] == 0
    assert daemon._handle_ipc({"action": "activity.list", "limit": 20}) == {
        "ok": True,
        "activity": [],
    }
    daemon.database.record_received(
        operation_id="activity_abcdefghijkl",
        correlation_id="correlation_abcdefgh",
        account_id=state.account_id,
        grant_id="grant_abcdefghijklmn",
        link_id=state.link_id,
        device_id=state.device_id,
        project_id=None,
        capability="project_shell",
        action_digest="a" * 64,
        idempotency_key="shell_activity_key",
        request_digest="b" * 64,
        request={"action": "start", "purpose": "Inspect activity", "command": "whoami"},
        connection_epoch=1,
        deadline_at="2030-01-01T00:00:00+00:00",
    )
    detail = daemon._handle_ipc(
        {"action": "activity.detail", "operation_id": "activity_abcdefghijkl"}
    )
    assert detail["ok"] is True
    assert detail["activity"]["request"]["command"] == "whoami"


@pytest.mark.asyncio
async def test_frontend_operation_waits_for_disconnect_cleanup_barrier() -> None:
    from codito_agent.daemon import CoditoDaemon

    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    adapter_called = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await finish_cleanup.wait()

    class Adapter:
        async def execute(self, *_args, **_kwargs):
            adapter_called.set()
            return ToolResponse({}, "done")

    daemon = CoditoDaemon.__new__(CoditoDaemon)
    daemon.adapter = Adapter()
    daemon._frontend_cleanup_task = asyncio.create_task(cleanup())
    await cleanup_started.wait()
    operation = asyncio.create_task(
        daemon._execute_operation(
            "project_frontend",
            {},
            "grant",
            "link",
            2,
            datetime.now(UTC) + timedelta(seconds=30),
            False,
        )
    )
    await asyncio.sleep(0)
    assert not adapter_called.is_set()
    finish_cleanup.set()
    await operation
    assert adapter_called.is_set()
    assert daemon._frontend_cleanup_task is None
