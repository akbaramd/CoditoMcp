from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from codito_protocol import (
    ListProjectsInput,
    ProjectApplyPatchResult,
    ProjectReadResult,
    ProjectShellResult,
    validate_project_apply_patch,
    validate_project_read,
    validate_project_shell,
)
from pydantic import TypeAdapter, ValidationError

from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .db import AgentDatabase
from .errors import AgentError
from .patching import PatchService
from .read_tools import ProjectReadService, ToolResponse, _decode_cursor, _encode_cursor
from .shell import ShellManager


class AgentProtocolAdapter:
    """Validate every request and result at the device trust boundary."""

    def __init__(
        self,
        database: AgentDatabase,
        reads: ProjectReadService,
        patches: PatchService,
        shells: ShellManager,
        *,
        device_id: str,
        account_id: str,
        approvals: ApprovalManager,
        read_concurrency: int = 4,
    ) -> None:
        self.database = database
        self.reads = reads
        self.patches = patches
        self.shells = shells
        self.device_id = device_id
        self.account_id = account_id
        self.approvals = approvals
        self.online = True
        self._read_semaphore = asyncio.Semaphore(read_concurrency)
        self._read_result: TypeAdapter[Any] = TypeAdapter(ProjectReadResult)
        self._shell_result: TypeAdapter[Any] = TypeAdapter(ProjectShellResult)

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
            if tool_name == "project_read":
                read_request = validate_project_read(payload)
                async with self._read_semaphore:
                    response = await asyncio.to_thread(self._read, read_request)
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
                        validated_cached = ProjectApplyPatchResult.model_validate(
                            cached.structured
                        )
                        return ToolResponse(
                            validated_cached.model_dump(mode="json", exclude_none=True),
                            cached.text,
                        )
                sections = self.patches.parser.parse(patch_request.patch)
                if not patch_request.dry_run and any(
                    section.action in {"delete", "move"} for section in sections
                ):
                    project = self.database.get_project(patch_request.project_id)
                    approval = ApprovalManager.build_request(
                        account_id=self.account_id,
                        grant_id=grant_id,
                        link_id=link_id,
                        device_id=self.device_id,
                        project_id=patch_request.project_id,
                        project_title=project.title,
                        capability="files:write",
                        action_digest=action_digest(patch_request),
                        connection_epoch=connection_epoch,
                        deadline_at=deadline_at,
                        risk=ApprovalRisk.DELETE,
                        summary="Delete one or more project files using an anchored patch",
                        patch=patch_request.patch,
                    )
                    await self.approvals.authorize_patch(approval, contains_delete=True)
                if deadline_at <= datetime.now(UTC):
                    raise AgentError(
                        "deadline_exceeded", "Operation deadline elapsed before patch commit"
                    )
                response = await asyncio.to_thread(
                    self.patches.apply,
                    **value,
                    account_id=self.account_id,
                    grant_id=grant_id,
                    link_id=link_id,
                    device_id=self.device_id,
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
            else:
                raise AgentError("invalid_request", "Unknown Codito tool")
        except ValidationError as exc:
            raise AgentError(
                "invalid_request",
                "Tool input or local result failed its protocol contract",
                {"errors": [error["type"] for error in exc.errors()[:8]]},
            ) from exc
        return ToolResponse(validated.model_dump(mode="json", exclude_none=True), response.text)

    def _read(self, request: Any) -> ToolResponse:
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
        response = self.reads.execute(value)
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
