"""Contracts for locally governed project registration metadata."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .read import Continuation, ProjectSummary
from .types import CoditoModel, OpaqueId


class GetProjectsInput(CoditoModel):
    operation: Literal["get_projects"] = "get_projects"
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=50, ge=1, le=100)


class RequestAddProjectInput(CoditoModel):
    operation: Literal["request_add_project"] = "request_add_project"
    title: str = Field(
        min_length=1,
        max_length=120,
        description="Suggested display title; the Windows user must select the local folder.",
    )
    idempotency_key: OpaqueId


class RenameProjectInput(CoditoModel):
    operation: Literal["rename_project"] = "rename_project"
    project_id: OpaqueId
    title: str = Field(min_length=1, max_length=120)
    idempotency_key: OpaqueId


class RemoveProjectInput(CoditoModel):
    operation: Literal["remove_project"] = "remove_project"
    project_id: OpaqueId
    idempotency_key: OpaqueId


type ProjectManageInput = Annotated[
    GetProjectsInput | RequestAddProjectInput | RenameProjectInput | RemoveProjectInput,
    Field(discriminator="operation"),
]


class GetProjectsResult(CoditoModel):
    operation: Literal["get_projects"] = "get_projects"
    projects: list[ProjectSummary]
    continuation: Continuation | None = None


class ProjectRegistrationRequestResult(CoditoModel):
    operation: Literal["request_add_project"] = "request_add_project"
    request_id: OpaqueId
    title: str
    status: Literal["pending_local_selection"] = "pending_local_selection"


class RenameProjectResult(CoditoModel):
    operation: Literal["rename_project"] = "rename_project"
    project_id: OpaqueId
    title: str
    status: Literal["renamed"] = "renamed"


class RemoveProjectResult(CoditoModel):
    operation: Literal["remove_project"] = "remove_project"
    project_id: OpaqueId
    title: str
    status: Literal["removed"] = "removed"


type ProjectManageResult = Annotated[
    GetProjectsResult
    | ProjectRegistrationRequestResult
    | RenameProjectResult
    | RemoveProjectResult,
    Field(discriminator="operation"),
]
