from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from .broker_client import BrokerClient
from .errors import AgentError
from .models import Project
from .paths import ProjectPathResolver
from .shell import _native_environment

MAX_CONFIG_BYTES = 256 * 1024
MAX_SERVER_OUTPUT_BYTES = 256 * 1024
SERVER_TIMEOUT_SECONDS = 1_800


class ProcessLike(Protocol):
    stdout: asyncio.StreamReader | None
    stderr: asyncio.StreamReader | None

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessStarter = Callable[[dict[str, Any], bool], Awaitable[ProcessLike]]
ReadyProbe = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class FrontendLaunchConfig:
    working_relative: str
    working_directory: Path
    command: str
    base_url: str
    source: str


@dataclass(slots=True)
class DevServerHandle:
    key: str
    project_id: str
    config: FrontendLaunchConfig
    owned: bool
    process: ProcessLike | None = None
    references: int = 1
    output: bytearray = field(default_factory=bytearray)
    output_truncated: bool = False
    readers: list[asyncio.Task[None]] = field(default_factory=list)


def loopback_origin(url: str) -> str:
    """Validate and return the exact HTTP(S) origin without credentials or secrets."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise AgentError("invalid_frontend_config", "Frontend baseUrl has an invalid port") from exc
    if parsed.scheme != "http":
        raise AgentError("invalid_frontend_config", "Frontend baseUrl must use loopback HTTP")
    if parsed.username is not None or parsed.password is not None:
        raise AgentError("invalid_frontend_config", "Frontend baseUrl cannot contain credentials")
    hostname = parsed.hostname or ""
    try:
        is_loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise AgentError("invalid_frontend_config", "Frontend baseUrl must use a loopback host")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise AgentError(
            "invalid_frontend_config", "Frontend baseUrl must be an origin without a path or query"
        )
    if port is None:
        port = 80
    if not 1 <= port <= 65_535:
        raise AgentError("invalid_frontend_config", "Frontend baseUrl has an invalid port")
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = port == 80
    return urlunsplit((parsed.scheme, host if default_port else f"{host}:{port}", "", "", ""))


class DevServerManager:
    """Discover, start, share and explicitly stop project-local development servers."""

    def __init__(
        self,
        resolver: ProjectPathResolver,
        broker: BrokerClient,
        data_directory: Path,
        *,
        process_starter: ProcessStarter | None = None,
        ready_probe: ReadyProbe | None = None,
        readiness_timeout: float = 45.0,
        stop_timeout: float = 5.0,
    ) -> None:
        self.resolver = resolver
        self.broker = broker
        self.data_directory = data_directory
        self._process_starter = process_starter or self._start_with_broker
        self._ready_probe = ready_probe or self._http_ready
        self._readiness_timeout = min(max(readiness_timeout, 1.0), 120.0)
        self._stop_timeout = min(max(stop_timeout, 0.01), 5.0)
        self._servers: dict[str, DevServerHandle] = {}
        self._stopping: dict[str, tuple[DevServerHandle, asyncio.Task[None]]] = {}
        self._guard = asyncio.Lock()

    async def _start_with_broker(
        self, specification: dict[str, Any], isolated: bool
    ) -> ProcessLike:
        return await self.broker.start_process(specification, isolated=isolated)

    async def _http_ready(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=1.5, trust_env=False
            ) as client:
                response = await client.get(url)
                return response.status_code < 500
        except httpx.HTTPError:
            return False

    def discover(self, project: Project) -> FrontendLaunchConfig:
        self.resolver.verify_project(project)
        for relative in (".codito/frontend.json", "codito.frontend.json"):
            raw = self._read_json(project, relative, optional=True)
            if raw is not None:
                return self._config_from_mapping(project, raw, relative)

        package = self._read_json(project, "package.json", optional=True)
        if package is None:
            raise AgentError(
                "frontend_config_required",
                "Add .codito/frontend.json with cwd, devCommand, and baseUrl",
            )
        nested: object = package.get("codito") if isinstance(package, dict) else None
        if isinstance(nested, dict) and isinstance(nested.get("frontend"), dict):
            return self._config_from_mapping(
                project, nested["frontend"], "package.json#codito.frontend"
            )
        return self._package_fallback(project, package)

    def _read_json(
        self, project: Project, relative: str, *, optional: bool
    ) -> dict[str, Any] | None:
        try:
            with self.resolver.open_read(project, relative) as stream:
                raw = stream.read(MAX_CONFIG_BYTES + 1)
        except AgentError as exc:
            if optional and exc.code == "path_not_found":
                return None
            raise
        if len(raw) > MAX_CONFIG_BYTES:
            raise AgentError("invalid_frontend_config", "Frontend configuration is too large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError(
                "invalid_frontend_config", "Frontend configuration is invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise AgentError("invalid_frontend_config", "Frontend configuration must be an object")
        return value

    def _config_from_mapping(
        self, project: Project, value: dict[str, Any], source: str
    ) -> FrontendLaunchConfig:
        cwd = value.get("cwd", ".")
        command = value.get("devCommand", value.get("dev_command"))
        base_url = value.get("baseUrl", value.get("base_url"))
        if (
            not isinstance(cwd, str)
            or not isinstance(command, str)
            or not isinstance(base_url, str)
        ):
            raise AgentError(
                "invalid_frontend_config",
                "Frontend config requires text cwd, devCommand, and baseUrl",
            )
        command = command.strip()
        if not command or len(command.encode("utf-8")) > 16_384 or "\x00" in command:
            raise AgentError("invalid_frontend_config", "Frontend devCommand is invalid")
        resolved = self.resolver.resolve(project, cwd or ".", directory=True, allow_root=True)
        return FrontendLaunchConfig(
            working_relative=resolved.relative,
            working_directory=resolved.absolute,
            command=command,
            base_url=loopback_origin(base_url),
            source=source,
        )

    def _package_fallback(self, project: Project, package: dict[str, Any]) -> FrontendLaunchConfig:
        scripts = package.get("scripts")
        if not isinstance(scripts, dict):
            raise AgentError(
                "frontend_config_required", "No deterministic frontend dev script was found"
            )
        script = (
            "dev"
            if isinstance(scripts.get("dev"), str)
            else "start"
            if isinstance(scripts.get("start"), str)
            else ""
        )
        if not script:
            raise AgentError(
                "frontend_config_required", "No deterministic frontend dev script was found"
            )

        lockfile_managers: list[tuple[str, str]] = []
        for lockfile, candidate in (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
            ("bun.lock", "bun"),
            ("package-lock.json", "npm"),
        ):
            try:
                self.resolver.resolve(project, lockfile, directory=False)
            except AgentError as exc:
                if exc.code == "path_not_found":
                    continue
                raise
            lockfile_managers.append((lockfile, candidate))
        if len(lockfile_managers) > 1:
            raise AgentError(
                "frontend_config_required",
                "Multiple package-manager lockfiles were found; add explicit frontend config",
            )
        manager = lockfile_managers[0][1] if lockfile_managers else "npm"
        command = f"{manager} run {script}"
        dependencies: dict[str, object] = {}
        for key in ("dependencies", "devDependencies"):
            dependency_group = package.get(key)
            if isinstance(dependency_group, dict):
                dependencies.update(dependency_group)
        if "@angular/core" in dependencies:
            port = 4200
        elif "vite" in dependencies:
            port = 5173
        elif "next" in dependencies or "react-scripts" in dependencies:
            port = 3000
        else:
            raise AgentError(
                "frontend_config_required",
                "Framework port is ambiguous; add .codito/frontend.json",
            )
        return FrontendLaunchConfig(
            working_relative=".",
            working_directory=project.root,
            command=command,
            base_url=f"http://localhost:{port}",
            source="package.json:auto",
        )

    async def acquire(
        self,
        project: Project,
        config: FrontendLaunchConfig,
        *,
        ready_timeout_seconds: float | None = None,
    ) -> DevServerHandle:
        key = f"{project.project_id}:{config.working_relative}:{config.command}:{config.base_url}"
        while True:
            async with self._guard:
                stopping = self._stopping.get(key)
                if stopping is not None and stopping[1].done():
                    task = stopping[1]
                    if not task.cancelled() and task.exception() is None:
                        self._stopping.pop(key, None)
                        stopping = None
            if stopping is None:
                break
            await asyncio.shield(stopping[1])
        async with self._guard:
            if key in self._stopping:
                raise AgentError(
                    "project_busy", "The previous managed frontend server is still stopping"
                )
            current = self._servers.get(key)
            if current is not None:
                alive = not current.owned or (
                    current.process is not None and current.process.returncode is None
                )
                if alive and await self._ready_probe(config.base_url):
                    current.references += 1
                    return current
                if current.owned:
                    await self._stop_handle(current)
                self._servers.pop(key, None)

            # A server that Codito did not launch is deliberately never terminated.
            if await self._ready_probe(config.base_url):
                if config.source == "package.json:auto":
                    raise AgentError(
                        "frontend_port_in_use",
                        "The auto-detected frontend port is already in use; add explicit config",
                    )
                handle = DevServerHandle(key, project.project_id, config, owned=False)
                self._servers[key] = handle
                return handle

            environment = _native_environment({}, self.data_directory)
            (self.data_directory / "temp").mkdir(parents=True, exist_ok=True)
            specification = {
                "project_root": str(project.root),
                "root_fingerprint": project.root_fingerprint,
                "working_directory": str(config.working_directory),
                "command": {"kind": "script", "shell": "powershell", "script": config.command},
                "environment": environment,
                "timeout_seconds": SERVER_TIMEOUT_SECONDS,
                "output_limit_bytes": MAX_SERVER_OUTPUT_BYTES,
                "nonce": secrets.token_hex(16),
                "external_working_directory_authorized": False,
            }
            process = await self._process_starter(specification, False)
            handle = DevServerHandle(key, project.project_id, config, owned=True, process=process)
            self._servers[key] = handle
            handle.readers = [
                asyncio.create_task(self._capture(handle, process.stdout)),
                asyncio.create_task(self._capture(handle, process.stderr)),
            ]
            try:
                await self._wait_until_ready(handle, ready_timeout_seconds=ready_timeout_seconds)
            except BaseException:
                self._servers.pop(key, None)
                # Transfer ownership to the retryable stopping registry before
                # awaiting cleanup. Repeated cancellation can interrupt this
                # caller, but cannot orphan the managed process.
                stop_task = self._start_stop_locked(handle)
                await asyncio.shield(stop_task)
                raise
            return handle

    async def _wait_until_ready(
        self, handle: DevServerHandle, *, ready_timeout_seconds: float | None
    ) -> None:
        loop = asyncio.get_running_loop()
        timeout = (
            self._readiness_timeout
            if ready_timeout_seconds is None
            else min(max(ready_timeout_seconds, 1.0), 120.0)
        )
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if handle.process is not None and handle.process.returncode is not None:
                raise AgentError(
                    "dev_server_failed", "The frontend development server exited early"
                )
            if await self._ready_probe(handle.config.base_url):
                return
            await asyncio.sleep(0.25)
        raise AgentError(
            "dev_server_timeout", "The frontend development server did not become ready"
        )

    async def _capture(self, handle: DevServerHandle, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            remaining = MAX_SERVER_OUTPUT_BYTES - len(handle.output)
            if remaining <= 0:
                handle.output_truncated = True
                continue
            handle.output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                handle.output_truncated = True

    async def release(self, handle: DevServerHandle) -> bool:
        async with self._guard:
            stopping = self._stopping.get(handle.key)
            if stopping is not None and stopping[0] is handle:
                task = stopping[1]
                if task.done() and (task.cancelled() or task.exception() is not None):
                    task = self._start_stop_locked(handle)
            else:
                current = self._servers.get(handle.key)
                if current is not handle:
                    return False
                current.references = max(0, current.references - 1)
                if current.references:
                    return False
                self._servers.pop(handle.key, None)
                if not current.owned:
                    return False
                task = self._start_stop_locked(current)
        await asyncio.shield(task)
        return True

    def _start_stop_locked(self, handle: DevServerHandle) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._stop_handle(handle), name=f"codito-dev-server-stop-{handle.project_id}"
        )
        self._stopping[handle.key] = (handle, task)

        def completed(done: asyncio.Task[None]) -> None:
            if done.cancelled() or done.exception() is not None:
                # Keep failed native cleanup quarantined and retryable.
                return
            existing = self._stopping.get(handle.key)
            if existing is not None and existing[1] is done:
                self._stopping.pop(handle.key, None)

        task.add_done_callback(completed)
        return task

    async def _wait_for_exit(self, process: ProcessLike) -> bool:
        try:
            await asyncio.wait_for(process.wait(), self._stop_timeout)
        except TimeoutError:
            return False
        except Exception:
            return False
        return process.returncode is not None

    async def _stop_handle(self, handle: DevServerHandle) -> None:
        process = handle.process
        if handle.owned and process is not None and process.returncode is None:
            try:
                process.terminate()
                exited = await self._wait_for_exit(process)
            except Exception:
                exited = False
            if not exited:
                try:
                    process.kill()
                except Exception as exc:
                    raise AgentError(
                        "dev_server_stop_failed",
                        "The managed frontend server could not be stopped",
                        retryable=True,
                    ) from exc
                if not await self._wait_for_exit(process):
                    raise AgentError(
                        "dev_server_stop_failed",
                        "The managed frontend server did not stop after termination",
                        retryable=True,
                    )
        for task in handle.readers:
            task.cancel()
        if handle.readers:
            await asyncio.gather(*handle.readers, return_exceptions=True)

    async def close(self) -> None:
        async with self._guard:
            handles = list(self._servers.values())
            self._servers.clear()
            tasks: list[asyncio.Task[None]] = []
            for handle, task in list(self._stopping.values()):
                if task.done() and (task.cancelled() or task.exception() is not None):
                    task = self._start_stop_locked(handle)
                tasks.append(task)
            for handle in handles:
                if handle.owned:
                    tasks.append(self._start_stop_locked(handle))
        if tasks:
            outcomes = await asyncio.gather(
                *(asyncio.shield(task) for task in tasks), return_exceptions=True
            )
            if any(isinstance(outcome, BaseException) for outcome in outcomes):
                raise AgentError(
                    "dev_server_stop_failed",
                    "One or more managed frontend servers could not be stopped",
                    retryable=True,
                )
