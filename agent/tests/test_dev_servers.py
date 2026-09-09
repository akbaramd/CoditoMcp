from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codito_agent.broker_client import BrokerClient
from codito_agent.db import AgentDatabase
from codito_agent.dev_servers import DevServerManager, loopback_origin
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode
from codito_agent.paths import ProjectPathResolver


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._returncode: int | None = None
        self.terminated = False

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        if self._returncode is None:
            await asyncio.Future()
        assert self._returncode is not None
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0

    def kill(self) -> None:
        self._returncode = -9


def test_loopback_origin_never_accepts_external_or_secret_urls() -> None:
    assert loopback_origin("http://localhost:5173") == "http://localhost:5173"
    for value in (
        "https://[::1]:4443/",
        "https://example.com",
        "http://user:secret@localhost:3000",
        "http://localhost:3000/path",
        "http://localhost:3000/?token=secret",
    ):
        with pytest.raises(AgentError):
            loopback_origin(value)


def test_explicit_frontend_config_wins_over_package_fallback(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    config_dir = project_root / ".codito"
    config_dir.mkdir()
    (config_dir / "frontend.json").write_text(
        json.dumps(
            {
                "cwd": ".",
                "devCommand": "dotnet run",
                "baseUrl": "http://localhost:5080",
            }
        ),
        encoding="utf-8",
    )
    (project_root / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "1"}}),
        encoding="utf-8",
    )
    manager = DevServerManager(ProjectPathResolver(), BrokerClient(None), tmp_path)
    config = manager.discover(project)
    assert config.command == "dotnet run"
    assert config.base_url == "http://localhost:5080"
    assert config.source == ".codito/frontend.json"


def test_package_fallback_rejects_multiple_lockfiles(tmp_path: Path, project_root: Path) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    (project_root / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "1"}}),
        encoding="utf-8",
    )
    (project_root / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    (project_root / "yarn.lock").write_text("", encoding="utf-8")
    manager = DevServerManager(ProjectPathResolver(), BrokerClient(None), tmp_path)
    with pytest.raises(AgentError, match="Multiple package-manager lockfiles"):
        manager.discover(project)


@pytest.mark.asyncio
async def test_reused_external_server_is_never_terminated(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    config_dir = project_root / ".codito"
    config_dir.mkdir()
    (config_dir / "frontend.json").write_text(
        json.dumps(
            {
                "cwd": ".",
                "devCommand": "npm run dev",
                "baseUrl": "http://localhost:5173",
            }
        ),
        encoding="utf-8",
    )
    starts = []

    async def start(specification, isolated):
        starts.append((specification, isolated))
        return FakeProcess()

    async def ready(_url: str) -> bool:
        return True

    manager = DevServerManager(
        ProjectPathResolver(),
        BrokerClient(None),
        tmp_path,
        process_starter=start,
        ready_probe=ready,
    )
    handle = await manager.acquire(project, manager.discover(project))
    assert not handle.owned
    assert await manager.release(handle) is False
    assert starts == []
    await manager.close()


@pytest.mark.asyncio
async def test_auto_detected_port_refuses_unverified_existing_listener(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    (project_root / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "1"}}),
        encoding="utf-8",
    )

    async def ready(_url: str) -> bool:
        return True

    manager = DevServerManager(
        ProjectPathResolver(), BrokerClient(None), tmp_path, ready_probe=ready
    )
    with pytest.raises(AgentError, match="explicit config"):
        await manager.acquire(project, manager.discover(project))
    await manager.close()


@pytest.mark.asyncio
async def test_managed_server_is_stopped_after_last_reference(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    (project_root / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "1"}}),
        encoding="utf-8",
    )
    process = FakeProcess()
    probes = iter((False, True))

    async def start(_specification, isolated):
        assert not isolated
        return process

    async def ready(_url: str) -> bool:
        return next(probes)

    manager = DevServerManager(
        ProjectPathResolver(),
        BrokerClient(None),
        tmp_path,
        process_starter=start,
        ready_probe=ready,
    )
    handle = await manager.acquire(project, manager.discover(project))
    assert handle.owned
    assert await manager.release(handle) is True
    assert process.terminated


class BlockingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_started = asyncio.Event()
        self.finish_wait = asyncio.Event()

    async def wait(self) -> int:
        self.wait_started.set()
        await self.finish_wait.wait()
        self._returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True


class FailsOnceStopProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.fail_stop = True

    def terminate(self) -> None:
        if self.fail_stop:
            raise OSError("terminate failed")
        super().terminate()

    def kill(self) -> None:
        if self.fail_stop:
            raise OSError("kill failed")
        super().kill()


@pytest.mark.asyncio
async def test_double_cancel_during_acquire_retains_owned_server_cleanup(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    (project_root / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "1"}}),
        encoding="utf-8",
    )
    process = BlockingProcess()

    async def start(_specification, _isolated):
        return process

    async def never_ready(_url: str) -> bool:
        return False

    manager = DevServerManager(
        ProjectPathResolver(),
        BrokerClient(None),
        tmp_path,
        process_starter=start,
        ready_probe=never_ready,
    )
    acquiring = asyncio.create_task(
        manager.acquire(project, manager.discover(project), ready_timeout_seconds=120)
    )
    for _ in range(20):
        if manager._servers:
            break
        await asyncio.sleep(0)
    assert manager._servers
    acquiring.cancel()
    await asyncio.wait_for(process.wait_started.wait(), 1)
    acquiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquiring

    assert not manager._servers
    assert manager._stopping
    process.finish_wait.set()
    for _ in range(20):
        if not manager._stopping:
            break
        await asyncio.sleep(0)
    assert process.terminated
    assert not manager._stopping
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_release_does_not_orphan_owned_server(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    (project_root / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "1"}}),
        encoding="utf-8",
    )
    process = BlockingProcess()
    probes = iter((False, True))

    async def start(_specification, _isolated):
        return process

    async def ready(_url: str) -> bool:
        return next(probes)

    manager = DevServerManager(
        ProjectPathResolver(),
        BrokerClient(None),
        tmp_path,
        process_starter=start,
        ready_probe=ready,
    )
    handle = await manager.acquire(project, manager.discover(project))
    releasing = asyncio.create_task(manager.release(handle))
    await process.wait_started.wait()
    releasing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await releasing
    process.finish_wait.set()
    for _ in range(20):
        if not manager._stopping:
            break
        await asyncio.sleep(0)
    assert process.terminated
    assert not manager._servers
    assert not manager._stopping
    await manager.close()


@pytest.mark.asyncio
async def test_failed_server_stop_is_quarantined_and_retryable(
    tmp_path: Path, project_root: Path
) -> None:
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("UI", project_root, ProjectMode.NATIVE_APPROVAL)
    (project_root / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "1"}}),
        encoding="utf-8",
    )
    process = FailsOnceStopProcess()
    probes = iter((False, True))

    async def start(_specification, _isolated):
        return process

    async def ready(_url: str) -> bool:
        return next(probes)

    manager = DevServerManager(
        ProjectPathResolver(),
        BrokerClient(None),
        tmp_path,
        process_starter=start,
        ready_probe=ready,
        stop_timeout=0.01,
    )
    handle = await manager.acquire(project, manager.discover(project))
    with pytest.raises(AgentError, match="could not be stopped"):
        await manager.release(handle)
    assert handle.key in manager._stopping

    process.fail_stop = False
    assert await manager.release(handle)
    await asyncio.sleep(0)
    assert handle.key not in manager._stopping
    await manager.close()
