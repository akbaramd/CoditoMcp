from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .access_policy import project_access_policy
from .approvals import ApprovalManager, ApprovalRequest, ApprovalRisk, action_digest
from .broker_client import BrokerClient
from .db import AgentDatabase
from .device_paths import WindowsReadScope
from .diagnostics import event
from .errors import AgentError
from .models import OperationState, Project, ProjectMode
from .operation_gate import ProjectOperationGate
from .paths import ProjectPathResolver
from .read_tools import ToolResponse
from .shell_policy import (
    executable_identity,
    outside_references,
    permission_scope,
    uncertain_constructs,
)


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
    PENDING_APPROVAL = "pending_approval"
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


def _native_environment(explicit: dict[str, str], data_directory: Path) -> dict[str, str]:
    """Standard user tool environment, not the Codito process's secrets/packager variables."""
    environment = _safe_environment(explicit, data_directory)
    standard = {
        "USERNAME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "COMMONPROGRAMFILES",
        "DOTNET_ROOT",
        "DOTNET_ROOT_X64",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "JAVA_HOME",
        "GOPATH",
        "GOBIN",
        "NVM_HOME",
        "NVM_SYMLINK",
    }
    for name, value in os.environ.items():
        if name.upper() in standard and name.upper() not in {key.upper() for key in explicit}:
            environment[name] = value
    path = os.environ.get("PATH", "")
    if os.name == "nt":
        import winreg

        # Read current installation paths, including tools installed after daemon start.
        paths: list[str] = []
        for hive, key in (
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "Path")
                    if isinstance(value, str):
                        paths.append(os.path.expandvars(value))
            except FileNotFoundError:
                continue
        if paths:
            path = os.pathsep.join(paths)
    # Do not implicitly search cwd, relative directories, or packager internals.
    directories = [
        part.strip('"')
        for part in path.split(os.pathsep)
        if part and Path(part.strip('"')).is_absolute()
    ]
    environment["PATH"] = os.pathsep.join(dict.fromkeys([environment["PATH"], *directories]))
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
                int(request.get("max_output_bytes", 256 * 1024)),
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
        project = await asyncio.to_thread(self.database.get_project, project_id)
        if not project.enabled:
            raise AgentError("project_disabled", "The requested project is disabled")
        # Remote execution selectors cannot activate a legacy disabled sandbox.
        project_access_policy(project, inside_project=True)
        execution = request.get("execution", "project_policy")
        if execution not in {"project_policy", "native_approval"}:
            raise AgentError("invalid_request", "Unknown execution policy request")
        mode = ProjectMode.NATIVE_APPROVAL if execution == "native_approval" else project.mode
        prior = await asyncio.to_thread(
            self.database.get_idempotency, project_id, "project_shell", key, **binding
        )
        if prior is not None:
            return self._existing_start(prior, digest)
        working_relative = str(request.get("working_directory", ".")) or "."
        external_cwd = request.get("external_working_directory")
        approval_timeout = int(request.get("approval_timeout_seconds", 180))
        start_wait_milliseconds = int(request.get("start_wait_milliseconds", 30_000))
        if not 15 <= approval_timeout <= 300:
            raise AgentError("invalid_request", "Approval timeout is out of range")
        if not 0 <= start_wait_milliseconds <= 30_000:
            raise AgentError("invalid_request", "Start wait is out of range")
        if external_cwd is not None:
            from codito_protocol.device_read import normalize_read_scope

            if working_relative != ".":
                raise AgentError("invalid_request", "Specify only one working directory")
            working_path = Path(normalize_read_scope(str(external_cwd)))
            if mode is ProjectMode.ISOLATED:
                raise AgentError(
                    "sandbox_policy_denied", "External cwd needs explicit native execution"
                )
        else:
            working_path = self.resolver.resolve(
                project, working_relative, directory=True, allow_root=True
            ).absolute
        references = outside_references(
            str(project.root),
            str(working_path),
            command,
            request.get("requested_external_paths", []),
        )
        uncertainty = uncertain_constructs(command)
        policy = project_access_policy(
            replace(project, mode=mode),
            inside_project=external_cwd is None and not references,
            uncertain=bool(uncertainty),
        )
        needs_approval = policy.requires_approval
        specification = self._build_specification(
            project,
            working_path,
            command,
            timeout,
            output_limit,
            native=mode is not ProjectMode.ISOLATED,
        )
        capabilities = await self.broker.probe()
        if mode is ProjectMode.ISOLATED and not capabilities.isolation_proven:
            raise AgentError(
                "sandbox_unavailable",
                "Isolated execution is disabled because confinement was not proven",
            )
        if mode is ProjectMode.ISOLATED and references:
            raise AgentError(
                "sandbox_policy_denied", "Isolated commands cannot request external paths"
            )
        # This is internal broker authority, never accepted from an MCP input.
        specification["external_working_directory_authorized"] = False
        executor = executable_identity(specification)
        approved_execution = {
            "project_id": project.project_id,
            "mode": mode.value,
            "command": specification["command"],
            "executor_identity": executor,
            "working_directory": specification["working_directory"],
            "environment": specification["environment"],
            "timeout_seconds": timeout,
            "output_limit_bytes": output_limit,
            "external_references": references,
            "uncertainty_reasons": uncertainty,
        }
        approval_generation = self.approvals.generation
        approval = ApprovalManager.build_request(
            account_id=self.account_id,
            grant_id=grant_id,
            link_id=link_id,
            device_id=self.device_id,
            project_id=project_id,
            project_title=project.title,
            capability="shell:execute",
            # Stable exact execution identity: purpose/idempotency key are not authority.
            action_digest=action_digest(approved_execution),
            connection_epoch=connection_epoch,
            deadline_at=datetime.now(UTC) + timedelta(seconds=approval_timeout)
            if needs_approval
            else deadline_at,
            risk=ApprovalRisk.NATIVE_EXECUTION,
            summary=(
                purpose
                if mode is ProjectMode.ISOLATED
                else purpose
                + "\nNative execution has the logged-in user's full filesystem and network "
                "authority; the working directory is not a security boundary."
                + ("\nReview reason: " + "; ".join(uncertainty) if uncertainty else "")
            ),
            command={**command, "resolved_executor_identity": executor},
            working_directory=str(working_path),
            environment_differences=specification["environment"],
            requested_network=mode is not ProjectMode.ISOLATED,
            requested_external_paths=references,
        )
        if deadline_at <= datetime.now(UTC):
            raise AgentError("deadline_exceeded", "Operation deadline elapsed before command start")

        async with self._guard:
            self.approvals.ensure_current(approval_generation, deadline_at)
            if deadline_at <= datetime.now(UTC):
                raise AgentError(
                    "deadline_exceeded", "Operation deadline elapsed before command start"
                )
            # Close the in-process race between the initial lookup and journal
            # insertion. Identical concurrent starts must never create two jobs.
            prior = await asyncio.to_thread(
                self.database.get_idempotency, project_id, "project_shell", key, **binding
            )
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
                    state=ShellState.PENDING_APPROVAL if needs_approval else ShellState.QUEUED,
                )
                self._jobs[job.job_id] = job
                stored = {
                    "action": "start",
                    "project_id": project_id,
                    "job_id": job.job_id,
                    "state": job.state,
                    "connection_epoch": connection_epoch,
                }
                await asyncio.to_thread(
                    self.database.put_idempotency,
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
                    self._run_job(
                        job,
                        specification,
                        mode is ProjectMode.ISOLATED,
                        approval=approval,
                        generation=approval_generation,
                        needs_approval=needs_approval,
                        persistent=policy.allow_saved_permissions
                        and self.approvals.supports_saved_permissions,
                        reuse_permission=policy.allow_saved_permissions,
                        external_cwd=external_cwd is not None,
                        original_mode=project.mode,
                        executor=executor,
                    ),
                    name=f"codito-shell-{job.job_id}",
                )
            except Exception:
                if job is not None:
                    self._jobs.pop(job.job_id, None)
                if journal_created:
                    await asyncio.to_thread(
                        self.database.delete_idempotency, project_id, "project_shell", key
                    )
                self.operation_gate.release(project_id, owner)
                raise
        assert job is not None
        await self._wait_for_start_transition(job, start_wait_milliseconds, deadline_at)
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
        *,
        native: bool = False,
    ) -> dict[str, Any]:
        kind = command.get("kind")
        explicit_environment = command.get("environment", {})
        if not isinstance(explicit_environment, dict):
            raise AgentError("invalid_request", "command environment must be an object")
        environment = (
            _native_environment(explicit_environment, self.data_directory)
            if native
            else _safe_environment(explicit_environment, self.data_directory)
        )
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

    async def _run_job(
        self,
        job: ShellJob,
        specification: dict[str, Any],
        isolated: bool,
        *,
        approval: ApprovalRequest,
        generation: int,
        needs_approval: bool,
        persistent: bool,
        reuse_permission: bool,
        external_cwd: bool,
        original_mode: ProjectMode,
        executor: dict[str, Any],
    ) -> None:
        pinned: WindowsReadScope | None = None
        try:
            await self._require_enabled(job.project_id)
            identity = specification["root_fingerprint"]
            if external_cwd:
                pinned = WindowsReadScope(specification["working_directory"]).__enter__()
                identity += ":" + pinned.identity
            if needs_approval:
                scope = (
                    permission_scope(
                        specification["working_directory"], approval.requested_external_paths
                    ),
                    identity,
                )
                await self.approvals.request(
                    approval,
                    session_eligible=False,
                    shell_scope=scope if persistent else None,
                    reuse_shell_permission=reuse_permission,
                )
                # Approval is a meaningful lifecycle transition of its own. Wake the
                # bounded start waiter before process creation so a slow native launch
                # cannot consume the MCP response budget after the user already decided.
                job.state = ShellState.QUEUED
                await self._notify(job)
            self.approvals.ensure_current(generation, approval.deadline_at)
            await self._require_enabled(job.project_id)
            current_project = await asyncio.to_thread(self.database.get_project, job.project_id)
            if current_project.mode is not original_mode:
                raise AgentError(
                    "approval_expired", "Project access mode changed before command start"
                )
            if needs_approval and executable_identity(specification) != executor:
                raise AgentError(
                    "approval_expired",
                    "Resolved executable changed; request fresh command approval",
                )
            specification["external_working_directory_authorized"] = external_cwd
            event("shell_starting", job.job_id)
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
            event("shell_failed", job.job_id, exc.code)
            self._append(job, "system", f"{exc.code}: {exc.message}\n")
            job.state = ShellState.FAILED
        except asyncio.CancelledError:
            if job.process is not None:
                await self._terminate(job.process)
            job.state = ShellState.CANCELLED
        except Exception:
            self._append(job, "system", "internal_error: command runner failed\n")
            job.state = ShellState.FAILED
        finally:
            if pinned is not None:
                pinned.close()
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
            await asyncio.to_thread(
                self.database.put_idempotency,
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
        max_output_bytes: int = 256 * 1024,
        *,
        grant_id: str,
        link_id: str,
    ) -> ToolResponse:
        if (
            cursor < 0
            or not 0 <= wait_milliseconds <= 30_000
            or not 16 * 1024 <= max_output_bytes <= 1024 * 1024
        ):
            raise AgentError(
                "invalid_request", "Poll cursor, wait, or output page size is out of range"
            )
        await self._require_enabled(project_id)
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
        result = self._poll_result(job, cursor, max_output_bytes)
        return ToolResponse(
            result,
            f"Shell job is {job.state}; returned {len(result['chunks'])} output chunk(s).",
        )

    @staticmethod
    def _poll_result(
        job: ShellJob, cursor: int, max_output_bytes: int = 256 * 1024
    ) -> dict[str, Any]:
        # Sequence numbers are contiguous and one-based, so the cursor is also
        # the exact list offset. Avoid rescanning all historical output on every
        # status call as a long-running build accumulates chunks.
        start_index = min(cursor, len(job.chunks))
        chunks: list[dict[str, Any]] = []
        page_bytes = 0
        for chunk in job.chunks[start_index:]:
            chunk_bytes = len(chunk.text.encode("utf-8"))
            if chunks and page_bytes + chunk_bytes > max_output_bytes:
                break
            chunks.append(chunk.as_dict())
            page_bytes += chunk_bytes
        next_cursor = chunks[-1]["sequence"] if chunks else cursor
        available_cursor = len(job.chunks)
        return {
            "action": "poll",
            "project_id": job.project_id,
            "job_id": job.job_id,
            "state": job.state,
            "chunks": chunks,
            "next_sequence_cursor": next_cursor,
            "available_sequence_cursor": available_cursor,
            "has_more_output": next_cursor < available_cursor,
            "exit_code": job.exit_code,
            "output_truncated": job.output_truncated,
        }

    async def cancel(
        self, project_id: str, job_id: str, *, grant_id: str, link_id: str
    ) -> ToolResponse:
        await self._require_enabled(project_id)
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
        await asyncio.to_thread(
            self.database.put_idempotency,
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

    async def _require_enabled(self, project_id: str) -> None:
        project = await asyncio.to_thread(self.database.get_project, project_id)
        if not project.enabled:
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

    @staticmethod
    async def _wait_for_start_transition(
        job: ShellJob, wait_milliseconds: int, deadline_at: datetime
    ) -> None:
        if wait_milliseconds <= 0 or job.state not in {
            ShellState.PENDING_APPROVAL,
            ShellState.QUEUED,
        }:
            return
        # Leave a small response margin inside the tunnel operation budget. The
        # shell job itself continues independently after this bounded wait.
        remaining_seconds = max(0.0, (deadline_at - datetime.now(UTC)).total_seconds() - 0.25)
        timeout_seconds = min(wait_milliseconds / 1000, remaining_seconds)
        if timeout_seconds <= 0:
            return
        initial_state = job.state
        async with job.changed:
            if job.state != initial_state:
                return
            try:
                await asyncio.wait_for(
                    job.changed.wait_for(lambda: job.state != initial_state),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                pass

    def disconnected(self) -> None:
        for job in self._jobs.values():
            if job.state == ShellState.PENDING_APPROVAL and job.task is not None:
                # Fence the approval task synchronously. Scheduling cancel() used
                # to leave one event-loop turn in which a just-released prompt
                # could reach the broker before cancellation acquired its async
                # database lane. _run_job owns durable cancellation finalization.
                job.task.cancel()
                continue
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
