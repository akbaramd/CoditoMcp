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
from codito_agent.shell import ShellManager, _native_environment, _safe_environment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY, ApprovalDecision.ALLOW_ALWAYS_READ],
)
async def test_legacy_isolated_cannot_be_activated_by_remote_native_selector(
    tmp_path: Path, project_root: Path, decision: ApprovalDecision
) -> None:
    database = AgentDatabase(tmp_path / "db.sqlite3")
    project = database.register_project("Example", project_root, ProjectMode.ISOLATED)
    prompted = []
    started = []

    async def prompt(request):
        assert request.requested_network
        assert request.working_directory == str(project_root)
        assert not request.persistent_read_eligible
        prompted.append(request)
        return decision

    async def start(specification, isolated):
        assert not isolated
        started.append(specification)
        return FakeProcess()

    manager = ShellManager(
        database,
        ProjectPathResolver(),
        FakeBroker(),
        ApprovalManager(prompt),
        tmp_path,
        account_id="account",
        device_id="device",
        process_starter=start,
    )
    request = {
        "project_id": project.project_id,
        "execution": "native_approval",
        "purpose": "Build with installed tools",
        "idempotency_key": "native_key_abcdefghijkl",
        "command": {"kind": "exec", "executable": "dotnet", "arguments": ["--version"]},
    }
    binding = dict(
        grant_id="grant",
        link_id="link",
        connection_epoch=1,
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    with pytest.raises(AgentError, match="sandbox_unavailable"):
        await manager.start(request, **binding)
    assert started == prompted == []
    assert database.get_project(project.project_id).mode is ProjectMode.ISOLATED


def test_native_environment_keeps_tools_but_not_process_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("USERNAME", "codito-test-user")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODITO_SECRET_KEY", "not-for-child")
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-child")
    environment = _native_environment({}, tmp_path)
    assert environment["USERNAME"] == "codito-test-user"
    assert environment["USERPROFILE"] == str(tmp_path)
    assert "CODITO_SECRET_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["PATH"]
    with pytest.raises(AgentError):
        _native_environment({"PATH": "attacker"}, tmp_path)


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
async def test_different_projects_execute_shell_jobs_in_parallel(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "parallel.sqlite3")
    first_project = database.register_project(
        "First project", project_root, ProjectMode.FULL_ACCESS
    )
    second_root = tmp_path / "second-project"
    second_root.mkdir()
    second_project = database.register_project(
        "Second project", second_root, ProjectMode.FULL_ACCESS
    )
    release = asyncio.Event()
    both_started = asyncio.Event()
    started_roots: set[str] = set()

    class BlockingProcess(FakeProcess):
        async def wait(self) -> int:
            await release.wait()
            self.returncode = 0
            return 0

    async def deny(_: Any) -> ApprovalDecision:
        raise AssertionError("full access must not prompt")

    async def start(specification: dict[str, Any], isolated: bool) -> BlockingProcess:
        assert not isolated
        started_roots.add(specification["project_root"])
        if len(started_roots) == 2:
            both_started.set()
        return BlockingProcess()

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
    binding = {
        "grant_id": "grant_abcdefghijklmn",
        "link_id": "link_abcdefghijklmnop",
        "connection_epoch": 2,
        "deadline_at": datetime.now(UTC) + timedelta(minutes=1),
    }

    async def start_project(project_id: str, key: str):
        return await manager.start(
            {
                "action": "start",
                "project_id": project_id,
                "purpose": "Parallel project regression test",
                "idempotency_key": key,
                "start_wait_milliseconds": 0,
                "command": {"kind": "exec", "executable": "uv", "arguments": ["--version"]},
            },
            **binding,  # type: ignore[arg-type]
        )

    first, second = await asyncio.gather(
        start_project(first_project.project_id, "parallel_first_key"),
        start_project(second_project.project_id, "parallel_second_key"),
    )
    await asyncio.wait_for(both_started.wait(), 1)
    assert started_roots == {str(project_root), str(second_root)}

    release.set()
    await asyncio.gather(
        manager._jobs[first.structured["job_id"]].task,
        manager._jobs[second.structured["job_id"]].task,
    )


@pytest.mark.asyncio
async def test_full_access_shell_start_poll_and_terminal_journal(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root, ProjectMode.FULL_ACCESS)

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
            "output_limit_bytes": 64 * 1024,
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
    job = manager._jobs[job_id]
    for character in ("a", "b", "c"):
        manager._append(job, "stdout", character * 8192)
    first_page = await manager.poll(
        project.project_id,
        job_id,
        1,
        max_output_bytes=16 * 1024,
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
    )
    assert [chunk["sequence"] for chunk in first_page.structured["chunks"]] == [2, 3]
    assert first_page.structured["next_sequence_cursor"] == 3
    assert first_page.structured["available_sequence_cursor"] == 4
    assert first_page.structured["has_more_output"] is True

    last_page = await manager.poll(
        project.project_id,
        job_id,
        3,
        max_output_bytes=16 * 1024,
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
    )
    assert [chunk["sequence"] for chunk in last_page.structured["chunks"]] == [4]
    assert last_page.structured["has_more_output"] is False
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

    restarted = ShellManager(
        database,
        ProjectPathResolver(),
        FakeBroker(),  # type: ignore[arg-type]
        ApprovalManager(deny),
        tmp_path,
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        process_starter=start,
    )
    recovered = await restarted.poll(
        project.project_id,
        job_id,
        0,
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
    )
    assert recovered.structured["state"] == "completed"
    assert recovered.structured["chunks"][0]["text"] == "hello\n"


def test_path_hijack_environment_override_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AgentError):
        _safe_environment({"Path": str(tmp_path)}, tmp_path)


@pytest.mark.asyncio
async def test_shell_job_and_idempotency_are_bound_to_oauth_grant(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root, ProjectMode.FULL_ACCESS)

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
    project = database.register_project("Example", project_root, ProjectMode.FULL_ACCESS)
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
    assert displayed[0].working_directory == str(project_root)
    assert "working directory is not a security boundary" in displayed[0].summary
