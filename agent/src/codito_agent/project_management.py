from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from codito_protocol import (
    GetProjectsInput,
    ProjectManageInput,
    RemoveProjectInput,
    RenameProjectInput,
    RequestAddProjectInput,
)

from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .db import AgentDatabase
from .read_tools import ToolResponse


class ProjectManagementService:
    def __init__(
        self,
        database: AgentDatabase,
        approvals: ApprovalManager,
        *,
        device_id: str,
        account_id: str,
        metadata_changed: Callable[[], None],
    ) -> None:
        self.database = database
        self.approvals = approvals
        self.device_id = device_id
        self.account_id = account_id
        self.metadata_changed = metadata_changed

    async def execute(
        self,
        request: ProjectManageInput,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        if isinstance(request, GetProjectsInput):
            projects = self.database.list_projects()
            result = {
                "operation": "get_projects",
                "projects": [
                    {
                        "project_id": project.project_id,
                        "title": project.title,
                        "device_id": self.device_id,
                        "mode": project.mode.value,
                        "online": True,
                    }
                    for project in projects[: request.limit]
                ],
            }
            return ToolResponse(result, f"Found {len(result['projects'])} registered project(s).")

        if isinstance(request, RequestAddProjectInput):
            request_id = self.database.create_project_registration_request(
                title=request.title,
                account_id=self.account_id,
                grant_id=grant_id,
                link_id=link_id,
                device_id=self.device_id,
                connection_epoch=connection_epoch,
            )
            return ToolResponse(
                {
                    "operation": "request_add_project",
                    "request_id": request_id,
                    "title": request.title,
                    "status": "pending_local_selection",
                },
                "The Windows user must select the project folder in Codito within 15 minutes.",
            )

        project = self.database.get_project(request.project_id)
        risk = (
            ApprovalRisk.DELETE if isinstance(request, RemoveProjectInput) else ApprovalRisk.WRITE
        )
        verb = "unregister" if isinstance(request, RemoveProjectInput) else "rename"
        approval = ApprovalManager.build_request(
            account_id=self.account_id,
            grant_id=grant_id,
            link_id=link_id,
            device_id=self.device_id,
            project_id=project.project_id,
            project_title=project.title,
            capability="projects:write",
            action_digest=action_digest(request),
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
            risk=risk,
            summary=f"Allow ChatGPT to {verb} the registered project metadata?",
        )
        await self.approvals.request(approval, session_eligible=False)
        if isinstance(request, RenameProjectInput):
            self.database.set_project_title(project.project_id, request.title)
            title = request.title.strip()
            status = "renamed"
            operation = "rename_project"
        else:
            self.database.set_project_enabled(project.project_id, enabled=False)
            title = project.title
            status = "removed"
            operation = "remove_project"
        self.metadata_changed()
        return ToolResponse(
            {
                "operation": operation,
                "project_id": project.project_id,
                "title": title,
                "status": status,
            },
            f"Project {status}; local source files were not changed.",
        )
