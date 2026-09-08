from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ProjectMode(StrEnum):
    ISOLATED = "isolated"
    NATIVE_APPROVAL = "native_approval"
    NATIVE_TRUSTED = "native_trusted"
    NATIVE_PROJECT = "native_project"


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    title: str
    root: Path
    root_fingerprint: str
    mode: ProjectMode = ProjectMode.ISOLATED
    enabled: bool = True

    def public_dict(self) -> dict[str, object]:
        """Return relay-safe metadata. Local paths are deliberately omitted."""

        return {
            "project_id": self.project_id,
            "title": self.title,
            "root_fingerprint": self.root_fingerprint,
            "mode": self.mode.value,
            "enabled": self.enabled,
        }


class OperationState(StrEnum):
    RECEIVED = "received"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


TERMINAL_OPERATION_STATES = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.DENIED,
        OperationState.CANCELLED,
        OperationState.OUTCOME_UNKNOWN,
    }
)
