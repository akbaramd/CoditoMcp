"""Locally selected project access policy; this never replaces OAuth or path checks.

The caller resolves the project from the local database and classifies the actual
operation, not an input-supplied 'inside' or 'approved' field. Native project mode
is cooperative approval routing, not operating-system confinement. A caller may
select another project it is authorized to use; this API does not invent an
authenticated conversation-wide active-project binding.
"""

from __future__ import annotations

from dataclasses import dataclass

from .approvals import ApprovalManager, ApprovalRequest
from .errors import AgentError
from .models import Project, ProjectMode


@dataclass(frozen=True, slots=True)
class LocalAccessPolicy:
    requires_approval: bool
    allow_saved_permissions: bool


def project_access_policy(
    project: Project, *, inside_project: bool, uncertain: bool = False
) -> LocalAccessPolicy:
    """Return local consent routing without changing trust or existing permissions."""
    if not project.enabled:
        raise AgentError("project_disabled", "The requested project is disabled")
    if project.mode is ProjectMode.ISOLATED:
        raise AgentError(
            "sandbox_unavailable",
            "Legacy isolated access is unavailable; choose a native access mode locally",
        )
    if project.mode is ProjectMode.FULL_ACCESS:
        return LocalAccessPolicy(requires_approval=False, allow_saved_permissions=False)
    if project.mode in {ProjectMode.NATIVE_APPROVAL, ProjectMode.NATIVE_TRUSTED}:
        # Old native_trusted consent covered native shell, not screens or all IO.
        # Never reinterpret a persisted value as the new explicit Full access grant.
        return LocalAccessPolicy(requires_approval=True, allow_saved_permissions=False)
    if project.mode is ProjectMode.NATIVE_PROJECT:
        return LocalAccessPolicy(
            requires_approval=not inside_project or uncertain, allow_saved_permissions=True
        )
    raise AgentError("invalid_request", "Unknown local project access mode")


async def authorize_project_operation(
    project: Project,
    approvals: ApprovalManager,
    request: ApprovalRequest,
    *,
    inside_project: bool,
    uncertain: bool = False,
    read_scope: tuple[str, str] | None = None,
    shell_scope: tuple[str, str] | None = None,
    screen_scope: tuple[str, str, str] | None = None,
) -> None:
    approvals.ensure_current(approvals.generation, request.deadline_at)
    policy = project_access_policy(project, inside_project=inside_project, uncertain=uncertain)
    if not policy.requires_approval:
        return
    await approvals.request(
        request,
        session_eligible=False,
        read_scope=read_scope if policy.allow_saved_permissions else None,
        shell_scope=shell_scope if policy.allow_saved_permissions else None,
        screen_scope=screen_scope if policy.allow_saved_permissions else None,
        reuse_shell_permission=policy.allow_saved_permissions,
    )
