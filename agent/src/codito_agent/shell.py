from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .broker_client import BrokerClient
from .db import AgentDatabase
from .errors import AgentError
from .models import OperationState, Project, ProjectMode
from .operation_gate import ProjectOperationGate
from .paths import ProjectPathResolver
from .read_tools import ToolResponse


class ProcessLike(Protocol):
    stdout: asyncio.StreamReader | None
    stderr: asyncio.StreamReader | None

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessStarter = Callable[[dict[str, Any], bool], Awaitable[ProcessLike]]


class ShellState:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"

    TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED, OUTCOME_UNKNOWN})


@dataclass(frozen=True, slots=True)
class OutputChunk:
    sequence: int
    stream: str
    text: str
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "stream": self.stream,
            "text": self.text,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class ShellJob:
    job_id: str
    project_id: str
    idempotency_key: str
    request_digest: str
    account_id: str
    grant_id: str
    link_id: str
    device_id: str
    connection_epoch: int
    timeout_seconds: int
    output_limit_bytes: int
    state: str = ShellState.QUEUED
    chunks: list[OutputChunk] = field(default_factory=list)
    output_bytes: int = 0
    output_truncated: bool = False
    exit_code: int | None = None
    process: ProcessLike | None = None
    task: asyncio.Task[None] | None = None
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    disconnected_killer: asyncio.Task[None] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _digest_request(request: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _safe_environment(explicit: dict[str, str], data_directory: Path) -> dict[str, str]:
    if len(explicit) > 64:
        raise AgentError("invalid_request", "At most 64 environment overrides are allowed")
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    safe_path = os.pathsep.join(
        (
            str(Path(system_root) / "System32"),
            str(Path(system_root)),
            str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"),
        )
    )
    environment = {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
        "PATH": safe_path,
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": str(data_directory / "temp"),
        "TMP": str(data_directory / "temp"),
    }
    protected = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP"}
    for name, value in explicit.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise AgentError("invalid_request", "Environment names and values must be text")
        if not name or "=" in name or "\x00" in name + value:
            raise AgentError("invalid_request", "Environment override is invalid")
        if name.upper() in protected:
            raise AgentError(
                "invalid_request", "Security-critical environment variables cannot be overridden"
            )
        if len(name) > 128 or len(value.encode()) > 8192:
            raise AgentError("invalid_request", "Environment override exceeds its size limit")
        environment[name] = value
    return environment


class ShellManager:
    def __init__(
        self,
        database: AgentDatabase,
        resolver: ProjectPathResolver,
        broker: BrokerClient,
        approvals: ApprovalManager,
        data_directory: Path,
        *,
        account_id: str,
        device_id: str,
        process_starter: ProcessStarter | None = None,
        operation_gate: ProjectOperationGate | None = None,
    ) -> None:
        self.database = database
        self.resolver = resolver
        self.broker = broker
        self.approvals = approvals
        self.data_directory = data_directory
        self.account_id = account_id
        self.device_id = device_id
        self._jobs: dict[str, ShellJob] = {}
        self._guard = asyncio.Lock()
        self.operation_gate = operation_gate or ProjectOperationGate()
        self._process_starter = process_starter or self._start_with_broker

    async def _start_with_broker(
        self, specification: dict[str, Any], isolated: bool
    ) -> ProcessLike:
        return await self.broker.start_process(specification, isolated=isolated)

    async def execute(
        self,
        request: dict[str, Any],
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        action = request.get("action")
        if action == "start":
            return await self.start(
                request,
                grant_id=grant_id,
                link_id=link_id,
                connection_epoch=connection_epoch,
                deadline_at=deadline_at,
            )
        if action == "poll":
            return await self.poll(
                str(request.get("project_id", "")),
                str(request.get("job_id", "")),
                int(request.get("sequence_cursor", 0)),
                int(request.get("wait_milliseconds", 0)),
                grant_id=grant_id,
                link_id=link_id,
            )
        if action == "cancel":
            return await self.cancel(
                str(request.get("project_id", "")),
                str(request.get("job_id", "")),
                grant_id=grant_id,
                link_id=link_id,
            )
        raise AgentError("invalid_request", "Unsupported project_shell action")

    async def start(
        self,
        request: dict[str, Any],
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        project_id = request.get("project_id")
        key = request.get("idempotency_key")
        purpose = request.get("purpose")
        command = request.get("command")
        if not isinstance(project_id, str) or not isinstance(key, str):
            raise AgentError("invalid_request", "project_id and idempotency_key are required")
        if not isinstance(purpose, str) or not purpose or len(purpose) > 1000:
            raise AgentError("invalid_request", "purpose must contain 1 to 1000 characters")
        if not isinstance(command, dict):
            raise AgentError("invalid_request", "command is required")
        timeout = int(request.get("timeout_seconds", 300))
        output_limit = int(request.get("output_limit_bytes", 2_097_152))
        if not 1 <= timeout <= 1800:
            raise AgentError("invalid_request", "timeout_seconds is out of range")
        if not 1024 <= output_limit <= 10_485_760:
            raise AgentError("invalid_request", "output_limit_bytes is out of range")
        digest = _digest_request(request)
        binding = {
            "account_id": self.account_id,
            "grant_id": grant_id,
            "link_id": link_id,
            "device_id": self.device_id,
        }
        project = self.database.get_project(project_id)
        if not project.enabled:
            raise AgentError("project_disabled", "The requested project is disabled")
        prior = self.database.get_idempotency(project_id, "project_shell", key, **binding)
        if prior is not None:
            return self._existing_start(prior, digest)
        working_relative = str(request.get("working_directory", ".")) or "."
        working = self.resolver.resolve(project, working_relative, directory=True, allow_root=True)
        specification = self._build_specification(
            project, working.absolute, command, timeout, output_limit
        )
        capabilities = await self.broker.probe()
        approval = ApprovalManager.build_request(
            account_id=self.account_id,
            grant_id=grant_id,
            link_id=link_id,
            device_id=self.device_id,
            project_id=project_id,
            project_title=project.title,
            capability="shell:execute",
            action_digest=action_digest(request),
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
            risk=ApprovalRisk.NATIVE_EXECUTION,
            summary=(
                purpose
                if project.mode is ProjectMode.ISOLATED
                else purpose
                + "\nNative execution has the logged-in user's full filesystem and network "
                "authority; the working directory is not a security boundary."
            ),
            command=command,
            working_directory=str(working.absolute),
            environment_differences=specification["environment"],
            requested_network=project.mode is not ProjectMode.ISOLATED,
            requested_external_paths=(
                ("logged-in user's accessible filesystem",)
                if project.mode is not ProjectMode.ISOLATED
                else ()
            ),
        )
        await self.approvals.authorize_shell(
            mode=project.mode,
            sandbox_proven=capabilities.isolation_proven,
            request=approval,
        )
        if deadline_at <= datetime.now(UTC):
            raise AgentError("deadline_exceeded", "Operation deadline elapsed before command start")

        async with self._guard:
            if deadline_at <= datetime.now(UTC):
                raise AgentError(
                    "deadline_exceeded", "Operation deadline elapsed before command start"
                )
            # Close the in-process race between the initial lookup and journal
            # insertion. Identical concurrent starts must never create two jobs.
            prior = self.database.get_idempotency(project_id, "project_shell", key, **binding)
            if prior is not None:
                return self._existing_start(prior, digest)
            owner = f"shell:{key}"
            self.operation_gate.reserve(project_id, owner)
            journal_created = False
            job: ShellJob | None = None
            try:
                job = ShellJob(
                    job_id=f"job_{secrets.token_urlsafe(24)}",
                    project_id=project_id,
                    idempotency_key=key,
                    request_digest=digest,
                    account_id=self.account_id,
                    grant_id=grant_id,
                    link_id=link_id,
                    device_id=self.device_id,
                    connection_epoch=connection_epoch,
                    timeout_seconds=timeout,
                    output_limit_bytes=output_limit,
                )
                self._jobs[job.job_id] = job
                stored = {
                    "action": "start",
                    "project_id": project_id,
                    "job_id": job.job_id,
                    "state": ShellState.QUEUED,
                    "connection_epoch": connection_epoch,
                }
                self.database.put_idempotency(
                    project_id,
                    "project_shell",
                    key,
                    digest,
                    OperationState.RUNNING,
                    stored,
                    **binding,
                )
                journal_created = True
                job.task = asyncio.create_task(
                    self._run_job(job, specification, project.mode is ProjectMode.ISOLATED),
                    name=f"codito-shell-{job.job_id}",
                )
            except Exception:
                if job is not None:
                    self._jobs.pop(job.job_id, None)
                if journal_created:
                    self.database.delete_idempotency(project_id, "project_shell", key)
                self.operation_gate.release(project_id, owner)
                raise
        assert job is not None
        return self._start_response(job)

    def _existing_start(self, prior: dict[str, Any], digest: str) -> ToolResponse:
        if prior["request_digest"] != digest:
            raise AgentError(
                "idempotency_conflict", "Idempotency key was reused for another command"
            )
        result = prior.get("result") or {}
        job_id = result.get("job_id")
        if isinstance(job_id, str) and job_id in self._jobs:
            return self._start_response(self._jobs[job_id])
        raise AgentError(
            "outcome_unknown",
            "The command may have started before the agent restarted and will not be replayed",
        )

    def _build_specification(
        self,
        project: Project,
        working_directory: Path,
        command: dict[str, Any],
        timeout: int,
        output_limit: int,
    ) -> dict[str, Any]:
        kind = command.get("kind")
        explicit_environment = command.get("environment", {})
        if not isinstance(explicit_environment, dict):
            raise AgentError("invalid_request", "command environment must be an object")
        environment = _safe_environment(explicit_environment, self.data_directory)
        (self.data_directory / "temp").mkdir(parents=True, exist_ok=True)
        if kind == "exec":
            executable = command.get("executable")
            arguments = command.get("arguments", [])
            if not isinstance(executable, str) or not executable or "\x00" in executable:
                raise AgentError("invalid_request", "Executable is invalid")
            if not isinstance(arguments, list) or not all(isinstance(v, str) for v in arguments):
                raise AgentError("invalid_request", "Arguments must be a list of text values")
            command_spec = {
                "kind": "exec",
                "executable": executable,
                "arguments": arguments,
            }
        elif kind == "script":
            shell = command.get("shell")
            script = command.get("script")
            if shell not in {"powershell", "cmd"} or not isinstance(script, str) or not script:
                raise AgentError("invalid_request", "Script command is invalid")
            if len(script.encode()) > 262_144 or "\x00" in script:
                raise AgentError("invalid_request", "Script exceeds its size limit")
            command_spec = {"kind": "script", "shell": shell, "script": script}
        else:
            raise AgentError("invalid_request", "Command kind must be exec or script")
        return {
            "project_root": str(project.root),
            "root_fingerprint": project.root_fingerprint,
            "working_directory": str(working_directory),
            "command": command_spec,
            "environment": environment,
            "timeout_seconds": timeout,
            "output_limit_bytes": output_limit,
            "nonce": uuid.uuid4().hex,
        }

    async def _run_job(self, job: ShellJob, specification: dict[str, Any], isolated: bool) -> None:
        try:
            process = await self._process_starter(specification, isolated)
            job.process = process
            job.state = ShellState.RUNNING
            await self._notify(job)
            readers = [
                asyncio.create_task(self._capture(job, process.stdout, "stdout")),
                asyncio.create_task(self._capture(job, process.stderr, "stderr")),
            ]
            try:
                job.exit_code = await asyncio.wait_for(process.wait(), job.timeout_seconds)
            except TimeoutError:
                await self._terminate(process)
                job.exit_code = process.returncode
                self._append(
                    job, "system", "Command timed out and its Job Object was terminated.\n"
                )
                job.state = ShellState.FAILED
            await asyncio.gather(*readers, return_exceptions=True)
            if job.state not in ShellState.TERMINAL:
                job.state = ShellState.COMPLETED if job.exit_code == 0 else ShellState.FAILED
        except AgentError as exc:
            self._append(job, "system", f"{exc.code}: {exc.message}\n")
            job.state = ShellState.FAILED
        except Exception:
            self._append(job, "system", "internal_error: command runner failed\n")
            job.state = ShellState.FAILED
        finally:
            async with self._guard:
                self.operation_gate.release(job.project_id, f"shell:{job.idempotency_key}")
            terminal_result = self._poll_result(job, 0)
            terminal_state = (
                OperationState.SUCCEEDED
                if job.state == ShellState.COMPLETED
                else OperationState.CANCELLED
                if job.state == ShellState.CANCELLED
                else OperationState.OUTCOME_UNKNOWN
                if job.state == ShellState.OUTCOME_UNKNOWN
                else OperationState.FAILED
            )
            self.database.put_idempotency(
                job.project_id,
                "project_shell",
                job.idempotency_key,
                job.request_digest,
                terminal_state,
                terminal_result,
                account_id=job.account_id,
                grant_id=job.grant_id,
                link_id=job.link_id,
                device_id=job.device_id,
            )
            await self._notify(job)

    async def _capture(
        self, job: ShellJob, stream: asyncio.StreamReader | None, stream_name: str
    ) -> None:
        if stream is None:
            return
        while True:
            raw = await stream.read(4096)
            if not raw:
                return
            self._append(job, stream_name, raw.decode("utf-8", errors="replace"))
            await self._notify(job)

    def _append(self, job: ShellJob, stream: str, text: str) -> None:
        encoded = text.encode("utf-8")
        remaining = job.output_limit_bytes - job.output_bytes
        if remaining <= 0:
            job.output_truncated = True
            return
        kept = encoded[:remaining]
        rendered = kept.decode("utf-8", errors="ignore")
        truncated = len(kept) < len(encoded)
        job.output_bytes += len(kept)
        if truncated:
            job.output_truncated = True
        if rendered:
            job.chunks.append(OutputChunk(len(job.chunks) + 1, stream, rendered, truncated))

    @staticmethod
    async def _notify(job: ShellJob) -> None:
        async with job.changed:
            job.changed.notify_all()

    async def poll(
        self,
        project_id: str,
        job_id: str,
        cursor: int,
        wait_milliseconds: int = 0,
        *,
        grant_id: str,
        link_id: str,
    ) -> ToolResponse:
        if cursor < 0 or not 0 <= wait_milliseconds <= 30_000:
            raise AgentError("invalid_request", "Poll cursor or wait is out of range")
        self._require_enabled(project_id)
        job = self._jobs.get(job_id)
        if job is None or job.project_id != project_id:
            raise AgentError("job_not_found", "Shell job was not found")
        self._validate_job_binding(job, grant_id=grant_id, link_id=link_id)
        if wait_milliseconds and cursor >= len(job.chunks) and job.state not in ShellState.TERMINAL:
            async with job.changed:
                try:
                    await asyncio.wait_for(job.changed.wait(), wait_milliseconds / 1000)
                except TimeoutError:
                    pass
        chunks = [chunk.as_dict() for chunk in job.chunks if chunk.sequence > cursor]
        result = self._poll_result(job, cursor)
        return ToolResponse(
            result, f"Shell job is {job.state}; returned {len(chunks)} output chunk(s)."
        )

    @staticmethod
    def _poll_result(job: ShellJob, cursor: int) -> dict[str, Any]:
        chunks = [chunk.as_dict() for chunk in job.chunks if chunk.sequence > cursor]
        next_cursor = chunks[-1]["sequence"] if chunks else cursor
        return {
            "action": "poll",
            "project_id": job.project_id,
            "job_id": job.job_id,
            "state": job.state,
            "chunks": chunks,
            "next_sequence_cursor": next_cursor,
            "exit_code": job.exit_code,
            "output_truncated": job.output_truncated,
        }

    async def cancel(
        self, project_id: str, job_id: str, *, grant_id: str, link_id: str
    ) -> ToolResponse:
        self._require_enabled(project_id)
        job = self._jobs.get(job_id)
        if job is None or job.project_id != project_id:
            result = {
                "action": "cancel",
                "project_id": project_id,
                "job_id": job_id,
                "state": "not_found",
            }
            return ToolResponse(result, "Shell job was not found.")
        self._validate_job_binding(job, grant_id=grant_id, link_id=link_id)
        if job.state in ShellState.TERMINAL:
            result = {
                "action": "cancel",
                "project_id": project_id,
                "job_id": job_id,
                "state": "already_terminal",
            }
            return ToolResponse(result, "Shell job had already reached a terminal state.")
        if job.process is not None:
            await self._terminate(job.process)
        job.state = ShellState.CANCELLED
        if job.task is not None and not job.task.done():
            job.task.cancel()
        async with self._guard:
            self.operation_gate.release(project_id, f"shell:{job.idempotency_key}")
        self.database.put_idempotency(
            job.project_id,
            "project_shell",
            job.idempotency_key,
            job.request_digest,
            OperationState.CANCELLED,
            self._poll_result(job, 0),
            account_id=job.account_id,
            grant_id=job.grant_id,
            link_id=job.link_id,
            device_id=job.device_id,
        )
        await self._notify(job)
        result = {
            "action": "cancel",
            "project_id": project_id,
            "job_id": job_id,
            "state": "cancelled",
        }
        return ToolResponse(result, "Shell job and its Job Object were cancelled.")

    def _validate_job_binding(self, job: ShellJob, *, grant_id: str, link_id: str) -> None:
        if (
            job.account_id != self.account_id
            or job.device_id != self.device_id
            or job.grant_id != grant_id
            or job.link_id != link_id
        ):
            raise AgentError(
                "binding_mismatch", "Shell job belongs to another authorization binding"
            )

    def _require_enabled(self, project_id: str) -> None:
        if not self.database.get_project(project_id).enabled:
            raise AgentError("project_disabled", "The requested project is disabled")

    @staticmethod
    async def _terminate(process: ProcessLike) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()

    def _start_response(self, job: ShellJob) -> ToolResponse:
        result = {
            "action": "start",
            "project_id": job.project_id,
            "job_id": job.job_id,
            "state": job.state,
            "connection_epoch": job.connection_epoch,
        }
        return ToolResponse(result, f"Shell job {job.job_id} is {job.state}.")

    def disconnected(self) -> None:
        for job in self._jobs.values():
            if job.state not in ShellState.TERMINAL and job.disconnected_killer is None:
                job.disconnected_killer = asyncio.create_task(self._kill_after_grace(job))

    def reconnected(self) -> None:
        for job in self._jobs.values():
            if job.disconnected_killer is not None:
                job.disconnected_killer.cancel()
                job.disconnected_killer = None

    async def _kill_after_grace(self, job: ShellJob) -> None:
        try:
            await asyncio.sleep(60)
            if job.process is not None and job.state not in ShellState.TERMINAL:
                await self._terminate(job.process)
                job.state = ShellState.OUTCOME_UNKNOWN
                self._append(job, "system", "Relay reconnect grace expired; job terminated.\n")
                await self._notify(job)
        except asyncio.CancelledError:
            return
