from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from codito_agent.broker_client import BrokerCapabilities, BrokerClient


@pytest.mark.asyncio
async def test_concurrent_first_probe_is_single_flight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "Codito.Broker.exe"
    executable.write_bytes(b"broker")
    client = BrokerClient(executable)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def invoke(_request: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        nonlocal calls
        assert timeout_seconds == 15
        calls += 1
        entered.set()
        await release.wait()
        return {
            "capabilities": {
                "job_object": True,
                "appcontainer_api": False,
                "filesystem_acl": False,
                "network_denial": False,
                "child_containment": True,
                "isolation_proven": False,
            }
        }

    monkeypatch.setattr(client, "_invoke", invoke)
    probes = [asyncio.create_task(client.probe()) for _ in range(8)]
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert calls == 1

    release.set()
    capabilities = await asyncio.gather(*probes)

    assert calls == 1
    assert all(item is capabilities[0] for item in capabilities)


@pytest.mark.asyncio
async def test_cancelled_run_request_recovers_and_kills_returned_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    drain_started = asyncio.Event()
    allow_drain = asyncio.Event()

    class Stdin:
        closed = False
        payload = b""

        def write(self, value: bytes) -> None:
            self.payload += value

        async def drain(self) -> None:
            drain_started.set()
            await allow_drain.wait()

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    class Process:
        stdin = Stdin()
        stdout = None
        stderr = None
        returncode: int | None = None
        killed = False
        waited = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.waited = True
            return self.returncode or 0

    process = Process()

    async def create_process(*_args: Any, **_kwargs: Any) -> Any:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    executable = tmp_path / "Codito.Broker.exe"
    executable.touch()
    client = BrokerClient(executable)
    client._capabilities = BrokerCapabilities(
        job_object=True,
        appcontainer_api=False,
        filesystem_acl=False,
        network_denial=False,
        child_containment=True,
        isolation_proven=False,
    )

    task = asyncio.create_task(client.start_process({"command": "dev"}, isolated=False))
    await asyncio.wait_for(drain_started.wait(), 1)
    task.cancel()
    allow_drain.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.stdin.payload
    assert process.stdin.closed
    assert process.killed
    assert process.waited
    assert not client._cleanup_tasks


@pytest.mark.asyncio
async def test_cancelled_broker_probe_kills_and_waits_for_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    communicate_entered = asyncio.Event()

    class Process:
        stdin = None
        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self, _payload: bytes):
            communicate_entered.set()
            await asyncio.Event().wait()

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.waited = True
            return -9

    process = Process()

    async def create_process(*_args: Any, **_kwargs: Any) -> Any:
        return process

    executable = tmp_path / "Codito.Broker.exe"
    executable.write_bytes(b"broker")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    client = BrokerClient(executable)
    task = asyncio.create_task(client.probe())
    await asyncio.wait_for(communicate_entered.wait(), 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed
    assert process.waited
    assert not client._cleanup_tasks
