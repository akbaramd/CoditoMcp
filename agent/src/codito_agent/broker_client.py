from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AgentError


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    job_object: bool
    appcontainer_api: bool
    filesystem_acl: bool
    network_denial: bool
    child_containment: bool
    isolation_proven: bool
    reason: str | None = None


class BrokerClient:
    """Narrow JSON/stdio client for the separately built .NET security broker."""

    def __init__(self, executable: Path | None) -> None:
        self.executable = executable
        self._capabilities: BrokerCapabilities | None = None

    async def probe(self, *, refresh: bool = False) -> BrokerCapabilities:
        if self._capabilities is not None and not refresh:
            return self._capabilities
        if self.executable is None or not self.executable.is_file():
            self._capabilities = BrokerCapabilities(
                False, False, False, False, False, False, "broker executable is unavailable"
            )
            return self._capabilities
        response = await self._invoke({"version": 1, "operation": "probe"}, timeout_seconds=15)
        try:
            capabilities = response["capabilities"]
            self._capabilities = BrokerCapabilities(
                job_object=bool(capabilities["job_object"]),
                appcontainer_api=bool(capabilities["appcontainer_api"]),
                filesystem_acl=bool(capabilities["filesystem_acl"]),
                network_denial=bool(capabilities["network_denial"]),
                child_containment=bool(capabilities["child_containment"]),
                isolation_proven=bool(capabilities["isolation_proven"]),
                reason=response.get("reason"),
            )
        except (KeyError, TypeError) as exc:
            raise AgentError("broker_protocol_error", "Broker probe response is malformed") from exc
        if self._capabilities.isolation_proven and not all(
            (
                self._capabilities.job_object,
                self._capabilities.appcontainer_api,
                self._capabilities.filesystem_acl,
                self._capabilities.network_denial,
                self._capabilities.child_containment,
            )
        ):
            raise AgentError("broker_protocol_error", "Broker made an inconsistent isolation claim")
        return self._capabilities

    async def start_process(
        self, specification: dict[str, Any], *, isolated: bool
    ) -> asyncio.subprocess.Process:
        capabilities = await self.probe()
        if isolated and not capabilities.isolation_proven:
            raise AgentError("sandbox_unavailable", "Broker has not proven isolated execution")
        if not capabilities.job_object or not capabilities.child_containment:
            raise AgentError(
                "broker_unavailable", "Broker cannot prove kill-on-close child containment"
            )
        if self.executable is None:
            raise AgentError("broker_unavailable", "Security broker executable is unavailable")
        request = {
            "version": 1,
            "operation": "run",
            "mode": "isolated" if isolated else "native",
            "specification": specification,
        }
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.executable),
                "--run-json",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000,
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps(request, separators=(",", ":")).encode())
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
            return process
        except OSError as exc:
            raise AgentError("broker_unavailable", "Security broker could not start") from exc

    async def _invoke(self, request: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        assert self.executable is not None
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.executable),
                "--json",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(json.dumps(request, separators=(",", ":")).encode()),
                timeout_seconds,
            )
        except (OSError, TimeoutError) as exc:
            raise AgentError("broker_unavailable", "Security broker did not respond") from exc
        if process.returncode != 0:
            raise AgentError("broker_failed", "Security broker rejected the request")
        try:
            response = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentError(
                "broker_protocol_error", "Security broker returned invalid JSON"
            ) from exc
        if not isinstance(response, dict):
            raise AgentError("broker_protocol_error", "Security broker returned invalid JSON")
        return response
