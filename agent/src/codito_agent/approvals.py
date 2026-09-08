from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .errors import AgentError
from .models import ProjectMode


class ApprovalDecision(StrEnum):
    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"


class ApprovalRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    NATIVE_EXECUTION = "native_execution"
    EXTERNAL_ACCESS = "external_access"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    account_id: str
    grant_id: str
    link_id: str
    device_id: str
    project_id: str
    project_title: str
    capability: str
    action_digest: str
    connection_epoch: int
    deadline_at: datetime
    risk: ApprovalRisk
    summary: str
    command: dict[str, Any] | None = None
    patch: str | None = None
    working_directory: str | None = None
    environment_differences: dict[str, str] | None = None
    requested_network: bool = False
    requested_external_paths: tuple[str, ...] = ()
    session_eligible: bool = False


@dataclass(slots=True)
class _SessionGrant:
    account_id: str
    grant_id: str
    link_id: str
    device_id: str
    project_id: str
    capability: str
    action_digest: str
    connection_epoch: int
    created_at: datetime
    last_used_at: datetime


Prompt = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]


def action_digest(value: Any) -> str:
    """Compute an approval digest using the shared canonicalizer when installed."""

    try:
        from codito_protocol import compute_action_digest

        return str(compute_action_digest(value))
    except ImportError:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()


class ApprovalManager:
    SESSION_MAX = timedelta(minutes=30)
    SESSION_IDLE = timedelta(minutes=10)

    def __init__(self, prompt: Prompt) -> None:
        self._prompt = prompt
        self._grants: list[_SessionGrant] = []
        self._lock = threading.Lock()
        self._security_generation = 0

    async def request(self, request: ApprovalRequest, *, session_eligible: bool) -> None:
        self._ensure_before_deadline(request)
        if self._has_session_grant(request):
            return
        if request.session_eligible != session_eligible:
            from dataclasses import replace

            request = replace(request, session_eligible=session_eligible)
        with self._lock:
            security_generation = self._security_generation
        remaining = (request.deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise AgentError("approval_expired", "The operation deadline elapsed before approval")
        try:
            decision = await asyncio.wait_for(self._prompt(request), timeout=remaining)
        except TimeoutError as exc:
            raise AgentError(
                "approval_expired", "The local approval expired before a decision"
            ) from exc
        self._ensure_before_deadline(request)
        with self._lock:
            if security_generation != self._security_generation:
                raise AgentError(
                    "approval_expired",
                    "The approval was invalidated by a security or connection change",
                )
        if decision is ApprovalDecision.DENY:
            raise AgentError("approval_denied", "The local user denied this operation")
        if decision is ApprovalDecision.ALLOW_SESSION:
            if not session_eligible:
                raise AgentError(
                    "invalid_approval",
                    "Session approval is unavailable for this operation",
                )
            self._add_session_grant(request)
        elif decision is not ApprovalDecision.ALLOW_ONCE:
            raise AgentError("invalid_approval", "The approval response was not recognized")

    async def authorize_shell(
        self,
        *,
        mode: ProjectMode,
        sandbox_proven: bool,
        request: ApprovalRequest,
    ) -> None:
        if mode is ProjectMode.ISOLATED:
            if not sandbox_proven:
                raise AgentError(
                    "sandbox_unavailable",
                    "Isolated execution is disabled because confinement was not proven",
                )
            if request.requested_network or request.requested_external_paths:
                raise AgentError(
                    "sandbox_policy_denied",
                    "MVP isolated commands cannot request network or external paths",
                )
            return
        if mode is ProjectMode.NATIVE_TRUSTED:
            # This mode is an explicit local opt-in to the logged-in user's full
            # authority. The UI must present that warning when the mode is set.
            return
        await self.request(request, session_eligible=False)

    async def authorize_patch(self, request: ApprovalRequest, *, contains_delete: bool) -> None:
        if contains_delete:
            await self.request(request, session_eligible=False)

    def _has_session_grant(self, request: ApprovalRequest) -> bool:
        now = datetime.now(UTC)
        with self._lock:
            self._grants[:] = [
                grant
                for grant in self._grants
                if now - grant.created_at <= self.SESSION_MAX
                and now - grant.last_used_at <= self.SESSION_IDLE
            ]
            for grant in self._grants:
                if (
                    grant.account_id,
                    grant.grant_id,
                    grant.link_id,
                    grant.device_id,
                    grant.project_id,
                    grant.capability,
                    grant.action_digest,
                    grant.connection_epoch,
                ) == (
                    request.account_id,
                    request.grant_id,
                    request.link_id,
                    request.device_id,
                    request.project_id,
                    request.capability,
                    request.action_digest,
                    request.connection_epoch,
                ):
                    grant.last_used_at = now
                    return True
        return False

    def _add_session_grant(self, request: ApprovalRequest) -> None:
        now = datetime.now(UTC)
        with self._lock:
            self._grants.append(
                _SessionGrant(
                    request.account_id,
                    request.grant_id,
                    request.link_id,
                    request.device_id,
                    request.project_id,
                    request.capability,
                    request.action_digest,
                    request.connection_epoch,
                    now,
                    now,
                )
            )

    def clear(self, reason: str = "security_event") -> int:
        """Clear all grants on lock/sleep/logout/restart/revoke/long disconnect."""

        del reason  # callers log only the enum-like reason, never request bodies
        with self._lock:
            count = len(self._grants)
            self._grants.clear()
            self._security_generation += 1
        return count

    def invalidate_pending(self, reason: str = "security_event") -> None:
        """Invalidate decisions from prompts opened before a connection/security event."""

        del reason
        with self._lock:
            self._security_generation += 1

    @staticmethod
    def _ensure_before_deadline(request: ApprovalRequest) -> None:
        deadline = request.deadline_at
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise AgentError("approval_expired", "The operation deadline elapsed before approval")

    @staticmethod
    def build_request(**values: Any) -> ApprovalRequest:
        return ApprovalRequest(request_id=f"approval_{secrets.token_urlsafe(24)}", **values)
