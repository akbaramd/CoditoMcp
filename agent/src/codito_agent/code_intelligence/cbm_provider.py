"""Scoped adapter for codebase-memory-mcp 0.9 CLI structural analysis.

Only an approved, content-verified source mirror is indexed. Every query names
one project in a private per-root cache. Raw backend paths, metadata and Cypher
are never exposed as MCP arguments or results. Graph evidence is structural,
not compiler truth, dynamic dispatch, runtime traces or test coverage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codito_protocol import code as c

from .graph_provider import GraphProvider, NullGraphProvider, _find_related_tests, _is_test_file
from .owned_process import OwnedProcessTree
from .snapshot import capture_snapshot
from .source import read_source_bytes, read_source_text
from .types import WorkspaceSnapshot

_OUTPUT_LIMIT = 4 * 1024 * 1024
_GRAPH_WARNING = (
    "Structural graph evidence; dynamic dispatch, reflection and cross-service "
    "effects may be incomplete"
)
_NODE_FIELDS = ("name", "qualified_name", "file_path", "start_line", "end_line", "signature")
_DEPENDENCY_EDGES = frozenset(
    {
        "CALLS",
        "IMPORTS",
        "USAGE",
        "REFERENCES",
        "TESTS",
        "TESTS_FILE",
        "IMPLEMENTS",
        "INHERITS",
        "EXTENDS",
    }
)


class CodebaseMemoryError(RuntimeError):
    """Categorical provider failure safe to return without stderr or local paths."""


@dataclass(frozen=True, slots=True)
class IndexInfo:
    selector: str
    nodes: int = 0
    edges: int = 0


@dataclass(frozen=True, slots=True)
class _Node:
    name: str
    qualified_name: str
    path: str
    start_line: int
    end_line: int
    signature: str
    kind: c.SymbolKind


def _quoted(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _projection(alias: str) -> str:
    return ",".join(f"{alias}.{field}" for field in _NODE_FIELDS) + f",labels({alias})"


def _node_match(alias: str, node: _Node) -> str:
    return (
        f"{alias}.qualified_name = {_quoted(node.qualified_name)} "
        f"AND {alias}.file_path = {_quoted(node.path)}"
    )


def _base(snapshot_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "provider": c.ProviderKind.GRAPH,
        "precision": c.Precision.STRUCTURAL,
        "captured_at": datetime.now(UTC).isoformat(),
        "warnings": [_GRAPH_WARNING],
        "complete": False,
    }


async def _read_bounded(stream: asyncio.StreamReader | None, *, keep: bool) -> bytes:
    if stream is None:
        raise CodebaseMemoryError("Graph process has no output channel")
    chunks: list[bytes] = []
    total = 0
    while block := await stream.read(65536):
        total += len(block)
        if total > _OUTPUT_LIMIT:
            raise CodebaseMemoryError("Graph process exceeded its bounded output size")
        if keep:
            chunks.append(block)
    return b"".join(chunks)


class CodebaseMemoryGraphProvider(GraphProvider):
    def __init__(self, root: Path, data_directory: Path, executable: Path | None = None) -> None:
        self._root = root
        key = hashlib.sha256(os.path.normcase(str(root.resolve())).encode()).hexdigest()[:32]
        runtime_name = "codebase-memory-mcp.exe" if os.name == "nt" else "codebase-memory-mcp"
        self._managed_executable = data_directory / "runtimes" / runtime_name
        self._cache = data_directory / "code-intelligence" / "workspaces" / key
        self._mirror = self._cache / "source"
        self._db = self._cache / "graph"
        self._selector = "codito-" + key
        self._executable = executable or self._discover_executable()
        self._lock = asyncio.Lock()
        self._snapshot: WorkspaceSnapshot | None = None
        self._mirrored: set[str] = set()
        self._indexed = False
        self._version: str | None = None

    def _discover_executable(self) -> Path | None:
        if self._managed_executable.is_file():
            return self._managed_executable.resolve()
        found = shutil.which("codebase-memory-mcp")
        if not found:
            return None
        resolved = Path(found).resolve()
        if resolved.is_relative_to(self._root.resolve()):
            return None  # A repository cannot impersonate an installed runtime.
        return resolved

    @property
    def available(self) -> bool:
        return self._executable is not None and self._executable.is_file()

    @property
    def version(self) -> str | None:
        return self._version

    def _env(self) -> dict[str, str]:
        allowed = {
            "systemroot",
            "windir",
            "path",
            "pathext",
            "temp",
            "tmp",
            "home",
            "userprofile",
            "localappdata",
            "appdata",
            "programfiles",
            "programfiles(x86)",
            "systemdrive",
        }
        env = {key: value for key, value in os.environ.items() if key.lower() in allowed}
        env.update(CBM_CACHE_DIR=str(self._db), CBM_ALLOWED_ROOT=str(self._mirror))
        return env

    async def _run(
        self, tool: str, arguments: dict[str, Any], *, budget_seconds: float = 25.0
    ) -> dict[str, Any]:
        if not self.available or self._executable is None:
            raise CodebaseMemoryError("Structural graph runtime is not installed")
        async with self._lock:
            process: asyncio.subprocess.Process | None = None
            owner: OwnedProcessTree | None = None
            tasks: list[asyncio.Task[bytes]] = []
            try:
                process = await asyncio.create_subprocess_exec(
                    str(self._executable),
                    "cli",
                    "--json",
                    tool,
                    cwd=str(self._mirror),
                    env=self._env(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **OwnedProcessTree.spawn_options(),
                )
                owner = OwnedProcessTree(process)
                if process.stdin is None:
                    raise CodebaseMemoryError("Graph process has no input channel")
                process.stdin.write(json.dumps(arguments, ensure_ascii=True).encode())
                await process.stdin.drain()
                process.stdin.close()
                tasks = [
                    asyncio.create_task(_read_bounded(process.stdout, keep=True)),
                    asyncio.create_task(_read_bounded(process.stderr, keep=False)),
                ]
                async with asyncio.timeout(budget_seconds):
                    stdout, _ = await asyncio.gather(*tasks)
                    await process.wait()
                if process.returncode != 0:
                    raise CodebaseMemoryError("Graph runtime rejected the analysis request")
                envelope = json.loads(stdout)
                if not isinstance(envelope, dict) or envelope.get("isError"):
                    raise CodebaseMemoryError("Graph runtime reported an analysis error")
                result = envelope.get("structuredContent")
                if not isinstance(result, dict):
                    raise CodebaseMemoryError("Graph runtime returned no structured result")
                return result
            except TimeoutError as exc:
                raise CodebaseMemoryError("Graph runtime reached its analysis time limit") from exc
            except (OSError, ValueError, BrokenPipeError) as exc:
                raise CodebaseMemoryError(
                    "Graph runtime failed its local process/JSON contract"
                ) from exc
            finally:
                if owner is not None:
                    await asyncio.shield(owner.stop())
                elif process is not None:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.wait_for(process.wait(), 3)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

    def _prepare_mirror(self, snapshot: WorkspaceSnapshot) -> None:
        self._mirror.mkdir(parents=True, exist_ok=True)
        self._db.mkdir(parents=True, exist_ok=True)
        for rel in sorted(self._mirrored - set(snapshot.file_states)):
            target = self._mirror / rel
            if target.is_symlink():
                raise CodebaseMemoryError("Private graph mirror contains an unsafe path")
            target.unlink(missing_ok=True)
        for rel, state in snapshot.file_states.items():
            target = self._mirror / rel
            parent = target.parent
            for part in (parent, *parent.parents):
                if part == self._cache.parent:
                    break
                if part.exists() and (
                    part.is_symlink() or (getattr(part.lstat(), "st_file_attributes", 0) & 0x400)
                ):
                    raise CodebaseMemoryError("Private graph mirror contains an unsafe directory")
            if target.is_symlink():
                raise CodebaseMemoryError("Private graph mirror contains an unsafe file")
            data = read_source_bytes(self._root, rel, snapshot)
            if hashlib.sha256(data).hexdigest() != state.content_hash:
                raise CodebaseMemoryError("Source changed while preparing graph mirror")
            parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".codito-write")
            temporary.write_bytes(data)
            os.replace(temporary, target)
        self._mirrored = set(snapshot.file_states)

    async def ensure_indexed(
        self, *, snapshot: WorkspaceSnapshot | None = None, full_rebuild: bool = False
    ) -> IndexInfo:
        self._snapshot = None
        snapshot = snapshot or await capture_snapshot(self._root)
        # Kept synchronous within the bounded query: no detached worker can continue
        # reading source after access is revoked. Per-file sizes are bounded by snapshot.
        self._prepare_mirror(snapshot)
        if full_rebuild and self._indexed:
            await self._run("delete_project", {"project": self._selector})
            self._indexed = False
        result = await self._run(
            "index_repository",
            {
                "repo_path": str(self._mirror),
                "name": self._selector,
                "mode": "fast",
                "persistence": False,
            },
        )
        if result.get("project") != self._selector or result.get("status") not in {
            "indexed",
            "up_to_date",
            "unchanged",
        }:
            raise CodebaseMemoryError(
                "Graph runtime did not index the requested workspace identity"
            )
        if int(result.get("skipped_count", 0) or 0) > 0:
            raise CodebaseMemoryError(
                "Graph runtime skipped sources; refusing a complete index claim"
            )
        self._snapshot = snapshot
        self._indexed = True
        return IndexInfo(
            self._selector, int(result.get("nodes", 0) or 0), int(result.get("edges", 0) or 0)
        )

    def _require_snapshot(self, snapshot_id: str) -> WorkspaceSnapshot:
        if self._snapshot is None or self._snapshot.generation != snapshot_id:
            raise CodebaseMemoryError(
                "Graph is not synchronized with the requested source snapshot"
            )
        return self._snapshot

    async def _rows(self, match: str, where: str, projection: str, limit: int) -> list[list[Any]]:
        query = (
            match
            + (" WHERE " + where if where else "")
            + " RETURN "
            + projection
            + f" LIMIT {limit}"
        )
        result = await self._run(
            "query_graph", {"project": self._selector, "query": query, "max_rows": limit}
        )
        rows = result.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            raise CodebaseMemoryError("Graph query returned an invalid row set")
        return rows

    def _node(self, row: list[Any]) -> _Node | None:
        if len(row) < 7 or self._snapshot is None:
            return None
        path = str(row[2] or "").replace("\\", "/")
        if path not in self._snapshot.file_states:
            return None
        try:
            labels = json.loads(str(row[6]))
            label = str(labels[0]) if isinstance(labels, list) and labels else "unknown"
            kind = (
                c.SymbolKind(label.lower())
                if label.lower() in c.SymbolKind._value2member_map_
                else c.SymbolKind.UNKNOWN
            )
            return _Node(
                str(row[0])[:512],
                str(row[1]),
                path,
                max(0, int(row[3] or 0)),
                max(0, int(row[4] or 0)),
                str(row[5] or "")[:8192],
                kind,
            )
        except (ValueError, TypeError, IndexError):
            return None

    async def _nodes(self, where: str, limit: int) -> list[_Node]:
        rows = await self._rows("MATCH (n)", where, _projection("n"), limit)
        return [node for row in rows if (node := self._node(row)) is not None and node.name]

    def _location(self, node: _Node, *, at_line: int | None = None) -> c.CodeLocation:
        line = at_line or node.start_line
        if line < 1:
            return c.CodeLocation(path=node.path)
        text = read_source_text(self._root, node.path, self._snapshot).split("\n")
        end = line if at_line else max(line, node.end_line)
        if end > len(text):
            return c.CodeLocation(path=node.path)  # Never fabricate a stale source range.
        return c.CodeLocation(
            path=node.path,
            range=c.CodeRange(
                start=c.CodePosition(line=line, character=1),
                end=c.CodePosition(line=end, character=len(text[end - 1]) + 1),
            ),
        )

    async def _symbol_at(self, path: str, line: int) -> _Node | None:
        nodes = await self._nodes(f"n.file_path = {_quoted(path)}", 1000)
        containing = [
            node
            for node in nodes
            if node.start_line > 0
            and node.start_line <= line <= node.end_line
            and node.kind not in {c.SymbolKind.FILE, c.SymbolKind.MODULE}
        ]
        return min(
            containing,
            key=lambda node: (node.end_line - node.start_line, -node.start_line),
            default=None,
        )

    async def symbol_search(
        self,
        query: str,
        *,
        path: str | None,
        kinds: Iterable[c.SymbolKind],
        max_results: int,
        snapshot_id: str,
    ) -> c.CodeSymbolSearchResult:
        self._require_snapshot(snapshot_id)
        where = "n.name =~ " + _quoted("(?i).*" + re.escape(query) + ".*")
        if path:
            where += " AND n.file_path = " + _quoted(path)
        nodes = await self._nodes(where, min(2000, max_results * 4 + 1))
        wanted = set(kinds)
        symbols = [
            c.SymbolInfo(name=node.name, kind=node.kind, location=self._location(node))
            for node in nodes
            if not wanted or node.kind in wanted
        ]
        return c.CodeSymbolSearchResult(
            **_base(snapshot_id),
            symbols=symbols[:max_results],
            total_matches=len(symbols),
            truncated=len(nodes) >= max_results or len(symbols) > max_results,
        )

    async def definition(self, path: str, line: int, snapshot_id: str) -> c.CodeDefinitionResult:
        self._require_snapshot(snapshot_id)
        where = f"n.file_path = {_quoted(path)} AND r.line = {line}"
        rows = await self._rows("MATCH (n)-[r:CALLS]->(m)", where, _projection("m"), 33)
        nodes = [node for row in rows if (node := self._node(row)) is not None]
        if not nodes:
            node = await self._symbol_at(path, line)
            nodes = [node] if node is not None and node.start_line == line else []
        result = c.CodeDefinitionResult(
            **_base(snapshot_id), definitions=[self._location(node) for node in nodes[:32]]
        )
        result.warnings.append(
            "Graph resolves declaration/call-line candidates, not exact column or overload identity"
        )
        if not nodes:
            result.precision = c.Precision.UNAVAILABLE
        return result

    async def hover(self, path: str, line: int, snapshot_id: str) -> c.CodeHoverResult:
        self._require_snapshot(snapshot_id)
        node = await self._symbol_at(path, line)
        result = c.CodeHoverResult(**_base(snapshot_id))
        if node:
            result.content = node.name + node.signature
            result.format = "plaintext"
            result.range = self._location(node).range
            result.warnings.append("Declaration signature only; no inferred expression type")
        else:
            result.precision = c.Precision.UNAVAILABLE
        return result

    async def references(
        self, path: str, line: int, include_declaration: bool, max_results: int, snapshot_id: str
    ) -> c.CodeReferencesResult:
        self._require_snapshot(snapshot_id)
        node = await self._symbol_at(path, line)
        if node is None:
            return c.CodeReferencesResult(
                **{**_base(snapshot_id), "precision": c.Precision.UNAVAILABLE}
            )
        rows = await self._rows(
            "MATCH (n)-[r]->(m)",
            _node_match("m", node),
            _projection("n") + ",r.line,type(r)",
            max_results + 1,
        )
        locations = [self._location(node)] if include_declaration else []
        for row in rows:
            origin = self._node(row)
            if origin is not None and len(row) >= 9 and str(row[8]) in _DEPENDENCY_EDGES:
                try:
                    at_line = int(row[7] or 0) or None
                except (ValueError, TypeError):
                    at_line = None
                locations.append(self._location(origin, at_line=at_line))
        result = c.CodeReferencesResult(
            **_base(snapshot_id),
            references=locations[:max_results],
            total_matches=len(locations),
            truncated=len(rows) > max_results or len(locations) > max_results,
        )
        result.warnings.append(
            "Static dependents of the containing declaration; use LSP for exact symbol references"
        )
        return result

    async def _walk(
        self, root: _Node, *, incoming: bool, labels: tuple[str, ...], max_depth: int, limit: int
    ) -> tuple[list[tuple[_Node, int]], bool]:
        queue = deque([(root, 0)])
        visited = {(root.path, root.qualified_name)}
        results: list[tuple[_Node, int]] = []
        truncated = False
        requests = 0
        while queue and len(results) < limit and requests < 24:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for label in labels:
                requests += 1
                where = _node_match("m" if incoming else "n", node)
                alias = "n" if incoming else "m"
                rows = await self._rows(
                    f"MATCH (n)-[r:{label}]->(m)",
                    where,
                    _projection(alias),
                    limit - len(results) + 1,
                )
                for row in rows:
                    neighbor = self._node(row)
                    if neighbor is None or (neighbor.path, neighbor.qualified_name) in visited:
                        continue
                    if len(results) >= limit:
                        truncated = True
                        break
                    visited.add((neighbor.path, neighbor.qualified_name))
                    results.append((neighbor, depth + 1))
                    queue.append((neighbor, depth + 1))
                if len(results) >= limit or requests >= 24:
                    break
        return results, truncated or bool(queue and requests >= 24)

    async def call_hierarchy(
        self, path: str, line: int, direction: str, max_depth: int, max_nodes: int, snapshot_id: str
    ) -> c.CodeCallHierarchyResult:
        self._require_snapshot(snapshot_id)
        node = await self._symbol_at(path, line)
        result = c.CodeCallHierarchyResult(**_base(snapshot_id))
        if node is None:
            result.precision = c.Precision.UNAVAILABLE
            return result
        result.root = c.CallItem(name=node.name, kind=node.kind, location=self._location(node))
        for incoming in (True, False):
            if direction != "both" and direction != ("incoming" if incoming else "outgoing"):
                continue
            nodes, truncated = await self._walk(
                node,
                incoming=incoming,
                labels=("CALLS",),
                max_depth=max_depth,
                limit=max_nodes - len(result.incoming_calls) - len(result.outgoing_calls),
            )
            items = [
                c.CallItem(
                    name=item.name, kind=item.kind, location=self._location(item), depth=depth
                )
                for item, depth in nodes
            ]
            if incoming:
                result.incoming_calls = items
            else:
                result.outgoing_calls = items
            result.truncated |= truncated
        return result

    async def type_hierarchy(
        self,
        path: str,
        line: int,
        direction: str,
        max_nodes: int,
        snapshot_id: str,
        max_depth: int = 3,
    ) -> c.CodeTypeHierarchyResult:
        self._require_snapshot(snapshot_id)
        node = await self._symbol_at(path, line)
        result = c.CodeTypeHierarchyResult(**_base(snapshot_id))
        if node is None:
            result.precision = c.Precision.UNAVAILABLE
            return result
        result.root = c.TypeItem(name=node.name, kind=node.kind, location=self._location(node))
        for incoming, field in ((False, "supertypes"), (True, "subtypes")):
            if direction not in {"both", field}:
                continue
            nodes, truncated = await self._walk(
                node,
                incoming=incoming,
                labels=("INHERITS", "IMPLEMENTS", "EXTENDS"),
                max_depth=max_depth,
                limit=min(100, max_nodes - len(result.supertypes) - len(result.subtypes)),
            )
            setattr(
                result,
                field,
                [
                    c.TypeItem(
                        name=item.name, kind=item.kind, location=self._location(item), depth=depth
                    )
                    for item, depth in nodes
                ],
            )
            result.truncated |= truncated
        return result

    async def implementations(
        self, path: str, line: int, max_results: int, snapshot_id: str
    ) -> c.CodeImplementationsResult:
        hierarchy = await self.type_hierarchy(
            path, line, "subtypes", min(200, max_results), snapshot_id
        )
        return c.CodeImplementationsResult(
            **_base(snapshot_id),
            implementations=[item.location for item in hierarchy.subtypes],
            truncated=hierarchy.truncated,
        )

    async def _file_neighbors(
        self, paths: set[str], *, incoming: bool, limit: int
    ) -> tuple[set[str], bool]:
        if not paths:
            return set(), False
        side, target = ("m", "n") if incoming else ("n", "m")
        where = " OR ".join(f"{side}.file_path = {_quoted(path)}" for path in sorted(paths)[:100])
        rows = await self._rows(
            "MATCH (n)-[r]->(m)",
            "(" + where + ")",
            f"{target}.file_path,type(r)",
            min(2000, limit * 8 + 1),
        )
        snapshot = self._snapshot
        result = {
            str(row[0])
            for row in rows
            if len(row) >= 2
            and str(row[1]) in _DEPENDENCY_EDGES
            and snapshot is not None
            and str(row[0]) in snapshot.file_states
            and str(row[0]) not in paths
        }
        return set(sorted(result)[:limit]), len(paths) > 100 or len(result) > limit or len(
            rows
        ) >= min(2000, limit * 8 + 1)

    async def impact(
        self, rel: str, snapshot: WorkspaceSnapshot, max_nodes: int, snapshot_id: str
    ) -> c.CodeImpactResult:
        self._require_snapshot(snapshot_id)
        visited, frontier = {rel}, {rel}
        items: list[c.ImpactNode] = []
        truncated = False
        for depth in range(1, 5):
            neighbors, limited = await self._file_neighbors(
                frontier, incoming=True, limit=max_nodes
            )
            frontier = neighbors - visited
            visited.update(frontier)
            for path in sorted(frontier):
                items.append(
                    c.ImpactNode(path=path, reason=f"Static graph dependent at distance {depth}")
                )
            truncated |= limited
            if not frontier or len(items) >= max_nodes:
                truncated |= len(items) > max_nodes
                break
        return c.CodeImpactResult(
            **_base(snapshot_id),
            dependents=items[:max_nodes],
            truncated=truncated or bool(frontier and depth == 4),
        )

    async def architecture(
        self, snapshot: WorkspaceSnapshot, max_packages: int, snapshot_id: str
    ) -> c.CodeArchitectureResult:
        self._require_snapshot(snapshot_id)
        base = await NullGraphProvider().architecture(snapshot, max_packages, snapshot_id)
        counts = Counter(str(Path(path).parent).replace("\\", "/") for path in snapshot.file_states)
        packages: list[c.PackageInfo] = []
        for directory, count in sorted(counts.items())[:max_packages]:
            paths = {
                path
                for path in snapshot.file_states
                if str(Path(path).parent).replace("\\", "/") == directory
            }
            dependencies, _ = await self._file_neighbors(paths, incoming=False, limit=100)
            groups = sorted(
                {str(Path(path).parent).replace("\\", "/") for path in dependencies} - {directory}
            )
            packages.append(
                c.PackageInfo(
                    name=directory if directory != "." else "root",
                    path=directory if directory != "." else sorted(paths)[0],
                    file_count=count,
                    dependencies=groups,
                )
            )
        result = c.CodeArchitectureResult(
            **_base(snapshot_id),
            packages=packages,
            manifests=base.manifests,
            truncated=len(counts) > max_packages,
        )
        result.warnings.append(
            "Packages are directory/manifest groups with observed edges, "
            "not inferred architectural layers"
        )
        return result

    async def dependencies(
        self,
        rel: str | None,
        snapshot: WorkspaceSnapshot,
        direction: str,
        max_results: int,
        snapshot_id: str,
    ) -> c.CodeDependenciesResult:
        self._require_snapshot(snapshot_id)
        result = await NullGraphProvider().dependencies(
            rel, snapshot, direction, max_results, snapshot_id
        )
        result.provider = c.ProviderKind.GRAPH
        result.warnings = [
            _GRAPH_WARNING,
            "External package transitives require lockfile/build resolution and are not inferred",
        ]
        if rel is None:
            return result
        roots = {
            path
            for path in snapshot.file_states
            if path == rel or path.startswith(rel.rstrip("/") + "/")
        }
        visited, frontier = set(roots), set(roots)
        found: list[c.DependencyInfo] = []
        for depth in range(1, 5 if direction != "direct" else 2):
            neighbors, limited = await self._file_neighbors(
                frontier, incoming=False, limit=max_results
            )
            frontier = neighbors - visited
            visited.update(frontier)
            if direction != "transitive" or depth > 1:
                found.extend(
                    c.DependencyInfo(
                        name=path,
                        path=path,
                        kind="source",
                        direction="direct" if depth == 1 else "transitive",
                    )
                    for path in sorted(frontier)
                )
            result.truncated |= limited
            if not frontier or len(found) >= max_results:
                break
        result.dependencies = (found + result.dependencies)[:max_results]
        result.truncated |= len(found) > max_results
        return result

    async def related_tests(
        self, rel: str, snapshot: WorkspaceSnapshot, max_results: int, snapshot_id: str
    ) -> c.CodeRelatedTestsResult:
        self._require_snapshot(snapshot_id)
        impact = await self.impact(rel, snapshot, min(500, max_results * 4), snapshot_id)
        graph_tests = [
            c.TestRelation(path=node.path, relationship="structural_call")
            for node in impact.dependents
            if _is_test_file(node.path)
        ]
        seen = {item.path for item in graph_tests}
        candidates = graph_tests + [
            item for item in _find_related_tests(rel, snapshot) if item.path not in seen
        ]
        result = c.CodeRelatedTestsResult(
            **_base(snapshot_id),
            tests=candidates[:max_results],
            truncated=impact.truncated or len(candidates) > max_results,
        )
        result.warnings.append(
            "Related tests are candidates from static relationships/names, not measured coverage"
        )
        return result
