from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import AgentError


class ProjectOperationGate:
    """Serialize mutations per physical workspace while preserving project parallelism.

    Callers use a stable root fingerprint rather than a relay project ID.  This
    matters for explicitly-authorized external targets: two otherwise unrelated
    project IDs can still address the same directory and must not mutate it at
    the same time.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._owners: dict[str, str] = {}

    def reserve(self, resource_key: str, owner: str) -> None:
        with self._guard:
            current = self._owners.get(resource_key)
            if current is not None:
                raise AgentError(
                    "project_busy",
                    "Another patch or shell job is already mutating the same workspace root",
                    {"reason": "mutation_conflict", "retry_after_ms": 250},
                    retryable=True,
                )
            self._owners[resource_key] = owner

    def release(self, resource_key: str, owner: str) -> None:
        with self._guard:
            if self._owners.get(resource_key) == owner:
                del self._owners[resource_key]

    @contextmanager
    def hold(self, resource_key: str, owner: str) -> Iterator[None]:
        self.reserve(resource_key, owner)
        try:
            yield
        finally:
            self.release(resource_key, owner)
