"""Per-project snapshot barrier and bounded lazy provider lifecycle.

Disk reconciliation precedes every query. A separate query lock spans refresh,
all context sections and the post-query verification. Engine readiness is not
compiler completeness. Metadata inspection never starts an analysis process.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from codito_protocol.code import CapabilityInfo

from ..errors import AgentError
from .cbm_provider import CodebaseMemoryError, CodebaseMemoryGraphProvider
from .graph_provider import GraphProvider, NullGraphProvider
from .lsp_client import LspServerProfile
from .lsp_provider import LspProvider
from .lsp_router import LspRouter
from .snapshot import capture_snapshot
from .types import WorkspaceSnapshot, WorkspaceStatus

IDLE_TTL_SECONDS = 300.0


class ProjectWorkspace:
    def __init__(
        self,
        project_id: str,
        project_root: Path,
        lsp_profile: LspServerProfile | None = None,
        graph_provider: GraphProvider | None = None,
        root_identity: str | None = None,
    ) -> None:
        self.project_id = project_id
        self.project_root = project_root
        self.root_identity = root_identity
        self.query_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._router = LspRouter(project_root, lsp_profile)
        self._graph = graph_provider or NullGraphProvider()
        self._snapshot: WorkspaceSnapshot | None = None
        self._synchronized_id: str | None = None
        self._status = WorkspaceStatus.NOT_STARTED
        self._last_used = time.monotonic()
        self._graph_error: str | None = None

    @property
    def status(self) -> WorkspaceStatus:
        return self._status

    @property
    def snapshot(self) -> WorkspaceSnapshot | None:
        return self._snapshot

    @property
    def graph_provider(self) -> GraphProvider:
        return self._graph

    @property
    def startup_error(self) -> str | None:
        return self._graph_error

    @property
    def graph_error(self) -> str | None:
        return self._graph_error

    @property
    def graph_available(self) -> bool:
        return isinstance(self._graph, CodebaseMemoryGraphProvider) and self._graph.available

    @property
    def graph_ready(self) -> bool:
        return bool(
            self.graph_available
            and self._snapshot is not None
            and self._synchronized_id == self._snapshot.generation
            and self._graph_error is None
        )

    @property
    def lsp_server_name(self) -> str | None:
        return ", ".join(self._router.server_names) or None

    def capability_info(self) -> CapabilityInfo:
        return self._router.capability_info()

    @asynccontextmanager
    async def lsp_for_document(self, path: str) -> AsyncIterator[LspProvider | None]:
        async with self._router.document_provider(path) as provider:
            yield provider

    async def lsp_for_workspace(self, paths: Iterable[str]) -> list[LspProvider]:
        return await self._router.workspace_providers(paths)

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def is_idle(self, ttl_seconds: float = IDLE_TTL_SECONDS) -> bool:
        return not self.query_lock.locked() and time.monotonic() - self._last_used > ttl_seconds

    async def ensure_ready(self, *, analyze: bool = True) -> WorkspaceSnapshot:
        async with self._lock:
            return await self._refresh(analyze=analyze, full=False, force=False)

    async def force_reindex(self, full: bool = False) -> WorkspaceSnapshot:
        async with self._lock:
            return await self._refresh(analyze=True, full=full, force=True)

    async def snapshot_is_current(self, snapshot: WorkspaceSnapshot) -> bool:
        current = await capture_snapshot(
            self.project_root, snapshot, expected_identity=self.root_identity
        )
        return current.file_states == snapshot.file_states

    async def _refresh(self, *, analyze: bool, full: bool, force: bool) -> WorkspaceSnapshot:
        self.touch()
        self._status = WorkspaceStatus.INDEXING
        try:
            for _attempt in range(2):
                candidate = await capture_snapshot(
                    self.project_root, self._snapshot, expected_identity=self.root_identity
                )
                previous = self._snapshot
                if (
                    previous is not None
                    and previous.file_states == candidate.file_states
                    and not force
                ):
                    candidate = previous
                need_sync = force or self._synchronized_id != candidate.generation
                if analyze and need_sync:
                    # v1 deliberately restarts active language servers on disk changes.
                    # This handles untracked/deleted/config files even when server watchers
                    # are incomplete; it favors correctness over warm-query latency.
                    await self._router.restart_all()
                    self._graph_error = None
                    if (
                        isinstance(self._graph, CodebaseMemoryGraphProvider)
                        and self._graph.available
                    ):
                        try:
                            await self._graph.ensure_indexed(snapshot=candidate, full_rebuild=full)
                        except CodebaseMemoryError:
                            self._graph_error = (
                                "Structural graph could not synchronize this snapshot"
                            )
                    verified = await capture_snapshot(
                        self.project_root, candidate, expected_identity=self.root_identity
                    )
                    if verified.file_states != candidate.file_states:
                        force = True
                        continue
                    self._synchronized_id = candidate.generation
                self._snapshot = candidate
                self._status = (
                    WorkspaceStatus.READY if self._graph_error is None else WorkspaceStatus.STALE
                )
                return candidate
            raise AgentError(
                "path_race",
                "Workspace kept changing while analysis was synchronizing",
                retryable=True,
            )
        except BaseException:
            self._status = WorkspaceStatus.STALE
            self._synchronized_id = None
            raise

    async def shutdown(self) -> None:
        async with self._lock:
            await self._router.shutdown()
            self._synchronized_id = None
            self._status = WorkspaceStatus.NOT_STARTED
