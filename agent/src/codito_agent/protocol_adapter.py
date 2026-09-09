from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from codito_protocol import (
    DeviceReadInput,
    DeviceReadResult,
    ListProjectsInput,
    ProjectApplyPatchResult,
    ProjectManageResult,
    ProjectReadResult,
    ProjectShellResult,
    validate_project_apply_patch,
    validate_project_code,
    validate_project_code_result,
    validate_project_manage,
    validate_project_read,
    validate_project_shell,
)
from codito_protocol.desktop_action import DeviceDesktopInput, DeviceDesktopResult
from codito_protocol.screenshot import DeviceScreenshotInput, ScreenshotToolResult
from pydantic import TypeAdapter, ValidationError

from .access_policy import authorize_project_operation
from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .code_intelligence import CodeIntelligenceManager
from .db import AgentDatabase
from .desktop_actions import DesktopActionQueue, DeviceDesktopService
from .device_read import DeviceReadService
from .errors import AgentError
from .file_targets import resolve_file_target
from .models import Project
from .patching import PatchService
from .project_management import ProjectManagementService
from .read_tools import ProjectReadService, ToolResponse, _decode_cursor, _encode_cursor
from .scoped_read_tools import read_scoped_files
from .screen_capture import DeviceScreenshotService, ScreenCaptureQueue
from .shell import ShellManager


async def _drain_file_worker(
    perform: Callable[[Callable[[], None]], ToolResponse],
) -> ToolResponse:
    """A cancelled coroutine must not release pinned handles under a live thread."""
    cancelled = threading.Event()

    def check_cancelled() -> None:
        if cancelled.is_set():
            raise AgentError("approval_expired", "File operation was cancelled before starting")

    def run() -> ToolResponse:
        check_cancelled()
        return perform(check_cancelled)

    worker = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        while not worker.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(worker)
        if not worker.cancelled():
            worker.exception()
        raise


class AgentProtocolAdapter:
    """Validate every request and result at the device trust boundary."""

    def __init__(
        self,
        database: AgentDatabase,
        reads: ProjectReadService,
        patches: PatchService,
        shells: ShellManager,
        management: ProjectManagementService | None = None,
        *,
        device_id: str,
        account_id: str,
        approvals: ApprovalManager,
        read_concurrency: int = 4,
        code_intelligence: CodeIntelligenceManager | None = None,
    ) -> None:
        self.database = database
        self.reads = reads
        self.patches = patches
        self.shells = shells
        self.management = management
        self.device_id = device_id
        self.account_id = account_id
        self.approvals = approvals
        self.code_intelligence = code_intelligence
        self.online = True
        self._read_semaphore = asyncio.Semaphore(read_concurrency)
        self._read_result: TypeAdapter[Any] = TypeAdapter(ProjectReadResult)
        self._shell_result: TypeAdapter[Any] = TypeAdapter(ProjectShellResult)
        self._manage_result: TypeAdapter[Any] = TypeAdapter(ProjectManageResult)
        self.device_reads = DeviceReadService(
            approvals, account_id=account_id, device_id=device_id, database=database
        )
        self.screen_queue = ScreenCaptureQueue()
        self.screenshots = DeviceScreenshotService(
            approvals,
            self.screen_queue,
            account_id=account_id,
            device_id=device_id,
            database=database,
        )
        self.desktop_queue = DesktopActionQueue()
        self.desktop_actions = DeviceDesktopService(
            approvals,
            self.desktop_queue,
            account_id=account_id,
            device_id=device_id,
            database=database,
        )

    async def execute(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
        reconcile_duplicate: bool = False,
    ) -> ToolResponse:
        try:
            validated: Any
            if tool_name == "device_desktop":
                response = await self.desktop_actions.execute(
                    DeviceDesktopInput.model_validate(payload),
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
                validated = DeviceDesktopResult.model_validate(response.structured)
            elif tool_name == "device_screenshot":
                async with self._read_semaphore:
                    response = await self.screenshots.execute(
                        DeviceScreenshotInput.model_validate(payload),
                        grant_id=grant_id,
                        link_id=link_id,
                        connection_epoch=connection_epoch,
                        deadline_at=deadline_at,
                    )
                validated = TypeAdapter(ScreenshotToolResult).validate_python(response.structured)
            elif tool_name == "device_read":
                device_request = DeviceReadInput.model_validate(payload)
                async with self._read_semaphore:
                    response = await self.device_reads.execute(
                        device_request,
                        grant_id=grant_id,
                        link_id=link_id,
                        connection_epoch=connection_epoch,
                        deadline_at=deadline_at,
                    )
                validated = DeviceReadResult.model_validate(response.structured)
            elif tool_name == "project_read":
                read_request = validate_project_read(payload)
                async with self._read_semaphore:
                    if isinstance(read_request, ListProjectsInput):
                        response = await asyncio.to_thread(self._read, read_request)
                    else:
                        response = await self._file_operation(
                            read_request,
                            grant_id=grant_id,
                            link_id=link_id,
                            connection_epoch=connection_epoch,
                            deadline_at=deadline_at,
                            mutation=False,
                        )
                validated = self._read_result.validate_python(response.structured)
            elif tool_name == "project_apply_patch":
                patch_request = validate_project_apply_patch(payload)
                value = patch_request.model_dump(mode="python")
                if reconcile_duplicate:
                    cached = await asyncio.to_thread(
                        self.patches.reconcile_duplicate,
                        **value,
                        account_id=self.account_id,
                        grant_id=grant_id,
                        link_id=link_id,
                        device_id=self.device_id,
                    )
                    if cached is not None:
                        validated_cached = ProjectApplyPatchResult.model_validate(cached.structured)
                        return ToolResponse(
                            validated_cached.model_dump(mode="json", exclude_none=True),
                            cached.text,
                        )
                response = await self._file_operation(
                    patch_request,
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                    mutation=True,
                )
                validated = ProjectApplyPatchResult.model_validate(response.structured)
            elif tool_name == "project_shell":
                shell_request = validate_project_shell(payload)
                response = await self.shells.execute(
                    shell_request.model_dump(mode="python"),
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
                validated = self._shell_result.validate_python(response.structured)
            elif tool_name == "project_manage":
                if self.management is None:
                    raise AgentError("invalid_request", "Project management is unavailable")
                manage_request = validate_project_manage(payload)
                response = await self.management.execute(
                    manage_request,
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
                validated = self._manage_result.validate_python(response.structured)
            elif tool_name == "project_code":
                response = await self._code_operation(
                    payload,
                    grant_id=grant_id,
                    link_id=link_id,
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                )
                validated = validate_project_code_result(response.structured)
            else:
                raise AgentError("invalid_request", "Unknown Codito tool")
        except ValidationError as exc:
            raise AgentError(
                "invalid_request",
                "Tool input or local result failed its protocol contract",
                {"errors": [error["type"] for error in exc.errors()[:8]]},
            ) from exc
        return ToolResponse(validated.model_dump(mode="json", exclude_none=True), response.text)

    async def _code_operation(
        self,
        payload: dict[str, Any],
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        from .paths import ProjectPathResolver

        if self.code_intelligence is None:
            raise AgentError("invalid_request", "Code intelligence is unavailable")
        request = validate_project_code(payload)
        project = self.database.get_project(request.project_id)
        generation = self.approvals.generation
        native = request.operation not in {"code_intelligence_status", "code_workspace_summary"}
        resolver = ProjectPathResolver()

        def ensure_authorized() -> None:
            self.approvals.ensure_current(generation, deadline_at)
            current = self.database.get_project(project.project_id)
            if (
                not current.enabled
                or current.mode != project.mode
                or current.root != project.root
                or current.root_fingerprint != project.root_fingerprint
            ):
                raise AgentError("approval_expired", "Local project analysis access changed")
            resolver.verify_project(project)

        ensure_authorized()
        approval = self.approvals.build_request(
            account_id=self.account_id,
            grant_id=grant_id,
            link_id=link_id,
            device_id=self.device_id,
            project_id=project.project_id,
            project_title=project.title,
            capability="shell:execute" if native else "files:read",
            action_digest=action_digest(request),
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
            risk=ApprovalRisk.NATIVE_EXECUTION if native else ApprovalRisk.READ,
            summary=(
                f"{request.operation}: {request.purpose}\nProject: {project.title}"
                + (
                    "\nMay start trusted native analysis tools and write private analysis cache."
                    if native
                    else ""
                )
            ),
        )
        await authorize_project_operation(
            project, self.approvals, approval, inside_project=True, uncertain=native
        )
        ensure_authorized()
        async with self._read_semaphore:
            ensure_authorized()
            task = asyncio.create_task(
                self.code_intelligence.execute(
                    request, project.root, root_identity=project.root_fingerprint
                )
            )
            try:
                while not task.done():
                    ensure_authorized()
                    await asyncio.wait({task}, timeout=0.2)
                result = await task
                ensure_authorized()
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        validated = validate_project_code_result(result.model_dump(mode="json", exclude_none=True))
        return ToolResponse(
            validated.model_dump(mode="json", exclude_none=True),
            f"Code intelligence: {request.operation}",
        )

    async def _file_operation(
        self,
        request: Any,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
        mutation: bool,
    ) -> ToolResponse:
        project = self.database.get_project(request.project_id)
        generation = self.approvals.generation
        self.approvals.ensure_current(generation, deadline_at)
        scope_path = request.scope_path
        with resolve_file_target(project, scope_path) as target:

            def ensure_policy() -> None:
                self.approvals.ensure_current(generation, deadline_at)
                current = self.database.get_project(project.project_id)
                if not current.enabled or current.mode != project.mode:
                    raise AgentError("approval_expired", "Local project access changed")

            external = not target.inside_project
            write = mutation and not request.dry_run
            sections = self.patches.parser.parse(request.patch) if mutation else ()
            deleting = write and any(section.action in {"delete", "move"} for section in sections)
            capability = "files:write" if write else "device:read" if external else "files:read"
            operation = "file_patch" if mutation else request.operation
            approval = self.approvals.build_request(
                account_id=self.account_id,
                grant_id=grant_id,
                link_id=link_id,
                device_id=self.device_id,
                project_id=project.project_id,
                project_title=project.title,
                capability=capability,
                action_digest=action_digest(request),
                connection_epoch=connection_epoch,
                deadline_at=deadline_at,
                risk=ApprovalRisk.DELETE
                if deleting
                else ApprovalRisk.WRITE
                if write
                else ApprovalRisk.READ,
                summary=(
                    f"{operation}: {request.purpose}\n"
                    f"Target root: {target.project.root}\n"
                    f"Path: {getattr(request, 'path', '(patch document)') or '(root)'}"
                ),
                patch=request.patch if mutation else None,
                requested_external_paths=(target.scope_path,)
                if external and target.scope_path
                else (),
            )
            read_scope = (
                (target.scope_path, target.scope_identity)
                if external
                and not mutation
                and target.scope_path
                and target.scope_identity
                and self.approvals.supports_saved_permissions
                else None
            )
            await authorize_project_operation(
                project,
                self.approvals,
                approval,
                inside_project=target.inside_project,
                read_scope=read_scope,
            )
            self.approvals.ensure_current(generation, deadline_at)
            if mutation:
                if target.pinned_scope is not None:
                    for section in sections:
                        target.pinned_scope.directory(str(Path(section.path).parent))
                        if section.destination:
                            target.pinned_scope.directory(str(Path(section.destination).parent))
                    # Windows atomic replacement requires delete sharing on the
                    # parent. Release read pins, then the patch resolver rechecks
                    # root/parent/target identities before every mutation.
                    target.pinned_scope.release_for_mutation()
                value = request.model_dump(mode="python")
                value["scope_path"] = target.scope_path

                def perform(check_cancelled: Callable[[], None]) -> ToolResponse:
                    def check() -> None:
                        check_cancelled()
                        ensure_policy()

                    check()
                    return self.patches.apply(
                        **value,
                        target_project=target.project if target.scope_path else None,
                        account_id=self.account_id,
                        grant_id=grant_id,
                        link_id=link_id,
                        device_id=self.device_id,
                        check=check,
                    )
            else:

                def perform(check_cancelled: Callable[[], None]) -> ToolResponse:
                    def check() -> None:
                        check_cancelled()
                        ensure_policy()

                    check()
                    if target.pinned_scope is not None:
                        value = request.model_dump(mode="python", exclude_none=True)
                        if request.operation == "read_file" and value.get("continuation"):
                            value.pop("start_line", None)
                            value.pop("end_line", None)
                        return self._normalize_read_response(
                            request, read_scoped_files(target.pinned_scope, value, check)
                        )
                    return self._read(request)

            result = await _drain_file_worker(perform)
            ensure_policy()
            return result

    def _read(self, request: Any, target_project: Project | None = None) -> ToolResponse:
        if isinstance(request, ListProjectsInput):
            projects = self.database.list_projects()
            offset = _decode_cursor(request.cursor)
            page = projects[offset : offset + request.limit]
            next_offset = offset + len(page)
            continuation = (
                {
                    "cursor": _encode_cursor(next_offset),
                    "remaining_estimate": len(projects) - next_offset,
                }
                if next_offset < len(projects)
                else None
            )
            value = {
                "operation": "list_projects",
                "projects": [
                    {
                        "project_id": project.project_id,
                        "title": project.title,
                        "device_id": self.device_id,
                        "mode": project.mode.value,
                        "online": self.online,
                    }
                    for project in page
                ],
                "continuation": continuation,
            }
            return ToolResponse(value, f"Found {len(page)} registered project(s).")

        value = request.model_dump(mode="python", exclude_none=True)
        if request.operation == "read_file" and value.get("continuation"):
            value["start_line"] = None
            value["end_line"] = None
        response = (
            self.reads.execute(value, target_project=target_project)
            if target_project is not None
            else self.reads.execute(value)
        )
        return self._normalize_read_response(request, response)

    @staticmethod
    def _normalize_read_response(request: Any, response: ToolResponse) -> ToolResponse:
        structured = dict(response.structured)
        if structured.get("path") == ".":
            structured["path"] = ""
        continuation = structured.get("continuation")
        structured["continuation"] = {"cursor": continuation} if continuation else None
        if request.operation == "list_directory":
            structured.pop("truncated", None)
            structured["entries"] = [
                {
                    "path": entry["path"],
                    "kind": entry["kind"],
                    "size": entry["size"],
                }
                for entry in structured["entries"]
            ]
        elif request.operation == "read_file":
            structured.pop("text", None)
        elif request.operation == "search_text":
            structured.pop("query", None)
            structured.pop("scanned_bytes", None)
        return ToolResponse(structured, response.text)
