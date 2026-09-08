from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentError(Exception):
    """A stable, wire-safe error.

    Details must never contain local absolute paths, tokens, command output, or
    file bodies. The daemon converts all unexpected exceptions to
    ``internal_error`` before they cross the device boundary.
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


def conflict(message: str, **details: Any) -> AgentError:
    return AgentError("conflict", message, details)
