from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import AgentError


class ProjectOperationGate:
    """Serialize patch and shell mutation lifecycles per project."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._owners: dict[str, str] = {}

    def reserve(self, project_id: str, owner: str) -> None:
        with self._guard:
            current = self._owners.get(project_id)
            if current is not None and current != owner:
                raise AgentError(
                    "project_busy",
                    "A patch or shell job is already active for this project",
                    retryable=True,
                )
            self._owners[project_id] = owner

    def release(self, project_id: str, owner: str) -> None:
        with self._guard:
            if self._owners.get(project_id) == owner:
                del self._owners[project_id]

    @contextmanager
    def hold(self, project_id: str, owner: str) -> Iterator[None]:
        self.reserve(project_id, owner)
        try:
            yield
        finally:
            self.release(project_id, owner)
