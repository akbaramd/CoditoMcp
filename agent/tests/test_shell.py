from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from codito_agent.approvals import ApprovalDecision, ApprovalManager, ApprovalRequest
from codito_agent.broker_client import BrokerCapabilities
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode
from codito_agent.paths import ProjectPathResolver
from codito_agent.shell import ShellManager, _safe_environment


class FakeBroker:
    async def probe(self) -> BrokerCapabilities:
        return BrokerCapabilities(True, False, False, False, True, False, "test")


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(b"hello\n")
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode: int | None = None

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -1

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.asyncio
async def test_native_trusted_shell_start_poll_and_terminal_journal(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root, ProjectMode.NATIVE_TRUSTED)

    async def deny(_: Any) -> ApprovalDecision:
        raise AssertionError("native trusted should not prompt")

    async def start(_: dict[str, Any], isolated: bool) -> FakeProcess:
        assert not isolated
        return FakeProcess()

    manager = ShellManager(
        database,
        ProjectPathResolver(),
        FakeBroker(),  # type: ignore[arg-type]
        ApprovalManager(deny),
        tmp_path,
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        process_starter=start,
    )
    response = await manager.start(
        {
            "action": "start",
            "project_id": project.project_id,
            "working_directory": "",
            "purpose": "Print greeting",
            "timeout_seconds": 30,
            "output_limit_bytes": 1024,
            "idempotency_key": "shell_key_abcdefghijkl",
            "command": {"kind": "exec", "executable": "cmd.exe", "arguments": ["/c", "echo"]},
        },
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
        connection_epoch=2,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    job_id = response.structured["job_id"]
    assert job_id.startswith("job_")
    await manager._jobs[job_id].task
    poll = await manager.poll(
        project.project_id,
        job_id,
        0,
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
    )
    assert poll.structured["state"] == "completed"
    assert poll.structured["chunks"][0]["text"] == "hello\n"
    journal = database.get_idempotency(
        project.project_id,
        "project_shell",
        "shell_key_abcdefghijkl",
        account_id="account_abcdefghijkl",
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
        device_id="device_abcdefghijkl",
    )
    assert journal is not None and journal["state"] == "succeeded"


def test_path_hijack_environment_override_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AgentError):
        _safe_environment({"Path": str(tmp_path)}, tmp_path)


@pytest.mark.asyncio
async def test_shell_job_and_idempotency_are_bound_to_oauth_grant(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root, ProjectMode.NATIVE_TRUSTED)

    async def deny(_: Any) -> ApprovalDecision:
        raise AssertionError("native trusted should not prompt")

    async def start(_: dict[str, Any], isolated: bool) -> FakeProcess:
        assert not isolated
        return FakeProcess()

    manager = ShellManager(
        database,
        ProjectPathResolver(),
        FakeBroker(),  # type: ignore[arg-type]
        ApprovalManager(deny),
        tmp_path,
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        process_starter=start,
    )
    request = {
        "action": "start",
        "project_id": project.project_id,
        "working_directory": "",
        "purpose": "Print greeting",
        "timeout_seconds": 30,
        "output_limit_bytes": 1024,
        "idempotency_key": "grant_bound_shell_key",
        "command": {"kind": "exec", "executable": "cmd.exe", "arguments": ["/c", "echo"]},
    }
    response = await manager.start(
        request,
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
        connection_epoch=2,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    job_id = response.structured["job_id"]
    await manager._jobs[job_id].task

    with pytest.raises(AgentError) as poll_error:
        await manager.poll(
            project.project_id,
            job_id,
            0,
            grant_id="grant_different000",
            link_id="link_abcdefghijklmnop",
        )
    assert poll_error.value.code == "binding_mismatch"
    with pytest.raises(AgentError) as link_error:
        await manager.poll(
            project.project_id,
            job_id,
            0,
            grant_id="grant_abcdefghijklmn",
            link_id="link_different00000",
        )
    assert link_error.value.code == "binding_mismatch"
    with pytest.raises(AgentError) as replay_error:
        await manager.start(
            request,
            grant_id="grant_different000",
            link_id="link_abcdefghijklmnop",
            connection_epoch=2,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    assert replay_error.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_shell_rejects_disabled_project(tmp_path: Path, project_root: Path) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root, ProjectMode.NATIVE_TRUSTED)
    database.disable_project(project.project_id)

    async def deny(_: Any) -> ApprovalDecision:
        raise AssertionError("disabled project must fail before approval")

    async def start(_: dict[str, Any], isolated: bool) -> FakeProcess:
        raise AssertionError("disabled project must fail before execution")

    manager = ShellManager(
        database,
        ProjectPathResolver(),
        FakeBroker(),  # type: ignore[arg-type]
        ApprovalManager(deny),
        tmp_path,
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        process_starter=start,
    )
    with pytest.raises(AgentError) as error:
        await manager.start(
            {
                "action": "start",
                "project_id": project.project_id,
                "working_directory": "",
                "purpose": "Must not run",
                "timeout_seconds": 30,
                "output_limit_bytes": 1024,
                "idempotency_key": "disabled_shell_key",
                "command": {"kind": "exec", "executable": "cmd.exe", "arguments": []},
            },
            grant_id="grant_abcdefghijklmn",
            link_id="link_abcdefghijklmnop",
            connection_epoch=2,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    assert error.value.code == "project_disabled"


@pytest.mark.asyncio
async def test_native_approval_discloses_full_host_and_network_authority(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root, ProjectMode.NATIVE_APPROVAL)
    displayed: list[ApprovalRequest] = []

    async def approve(value: ApprovalRequest) -> ApprovalDecision:
        displayed.append(value)
        return ApprovalDecision.ALLOW_ONCE

    async def start(_: dict[str, Any], isolated: bool) -> FakeProcess:
        assert not isolated
        return FakeProcess()

    manager = ShellManager(
        database,
        ProjectPathResolver(),
        FakeBroker(),  # type: ignore[arg-type]
        ApprovalManager(approve),
        tmp_path,
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        process_starter=start,
    )
    response = await manager.start(
        {
            "action": "start",
            "project_id": project.project_id,
            "working_directory": "",
            "purpose": "Run a native build",
            "timeout_seconds": 30,
            "output_limit_bytes": 1024,
            "idempotency_key": "native_warning_key12",
            "command": {"kind": "exec", "executable": "cmd.exe", "arguments": ["/c"]},
        },
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
        connection_epoch=2,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    await manager._jobs[response.structured["job_id"]].task
    assert len(displayed) == 1
    assert displayed[0].requested_network is True
    assert displayed[0].requested_external_paths
    assert "working directory is not a security boundary" in displayed[0].summary
