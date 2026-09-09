"""Trusted multi-language LSP discovery and routing.

Only a fixed allowlist of locally installed language-server executable names is
auto-discovered. Repository-local executables are rejected. A project-specific
profile stored in Codito's private configuration directory remains supported as
an explicit local override/fallback.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from codito_protocol.code import CapabilityInfo
from lsprotocol import types as lsp

from ..errors import AgentError
from .lsp_client import LspClientError, LspServerProfile, ManagedLspClient
from .lsp_provider import LspProvider
from .source import read_source_text


@dataclass(frozen=True, slots=True)
class _ServerSpec:
    key: str
    extensions: frozenset[str]
    executables: tuple[str, ...]
    args: tuple[str, ...]
    language_id: str


_SPECS: tuple[_ServerSpec, ...] = (
    _ServerSpec(
        "python",
        frozenset({".py", ".pyi"}),
        ("basedpyright-langserver", "pyright-langserver"),
        ("--stdio",),
        "python",
    ),
    _ServerSpec(
        "typescript",
        frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}),
        ("typescript-language-server",),
        ("--stdio",),
        "typescript",
    ),
    _ServerSpec("csharp", frozenset({".cs"}), ("csharp-ls",), (), "csharp"),
    _ServerSpec("go", frozenset({".go"}), ("gopls",), ("serve",), "go"),
    _ServerSpec("rust", frozenset({".rs"}), ("rust-analyzer",), (), "rust"),
    _ServerSpec(
        "clang",
        frozenset({".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx"}),
        ("clangd",),
        (),
        "cpp",
    ),
    _ServerSpec("java", frozenset({".java"}), ("jdtls",), (), "java"),
    _ServerSpec("kotlin", frozenset({".kt", ".kts"}), ("kotlin-language-server",), (), "kotlin"),
    _ServerSpec("lua", frozenset({".lua"}), ("lua-language-server",), (), "lua"),
    _ServerSpec("ruby", frozenset({".rb"}), ("ruby-lsp",), (), "ruby"),
    _ServerSpec("php", frozenset({".php"}), ("phpactor",), ("language-server",), "php"),
    _ServerSpec(
        "dart", frozenset({".dart"}), ("dart",), ("language-server", "--protocol=lsp"), "dart"
    ),
    _ServerSpec("swift", frozenset({".swift"}), ("sourcekit-lsp",), (), "swift"),
    _ServerSpec(
        "bash",
        frozenset({".sh", ".bash", ".zsh"}),
        ("bash-language-server",),
        ("start",),
        "shellscript",
    ),
)

logger = logging.getLogger(__name__)

_LANGUAGE_ID_BY_EXTENSION = {
    extension: spec.language_id for spec in _SPECS for extension in spec.extensions
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _resolve_executable(name: str, root: Path) -> str | None:
    candidate = Path(name)
    resolved: Path | None = None
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
    else:
        found = shutil.which(name)
        if found:
            try:
                resolved = Path(found).resolve(strict=True)
            except OSError:
                return None
    if resolved is None or not resolved.is_file() or _inside(resolved, root):
        return None
    return str(resolved)


class LspRouter:
    """Lazily starts one trusted language server per language family."""

    def __init__(self, root: Path, manual_profile: LspServerProfile | None = None) -> None:
        self._root = root.resolve()
        self._manual_profile = manual_profile
        self._lock = asyncio.Lock()
        self._sessions: dict[str, tuple[ManagedLspClient, LspProvider]] = {}
        self._failures: set[str] = set()

    @property
    def server_names(self) -> list[str]:
        return [client._profile.name for client, _ in self._sessions.values()]

    @property
    def failures(self) -> list[str]:
        return sorted(self._failures)

    def capability_info(self) -> CapabilityInfo:
        infos = [provider.capability_info() for _, provider in self._sessions.values()]
        if not infos:
            return CapabilityInfo()
        names = CapabilityInfo.model_fields
        return CapabilityInfo(
            **{name: any(bool(getattr(info, name)) for info in infos) for name in names}
        )

    @staticmethod
    def language_id(path: str) -> str:
        extension = Path(path).suffix.lower()
        overrides = {
            ".js": "javascript",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".jsx": "javascriptreact",
            ".tsx": "typescriptreact",
            ".c": "c",
        }
        return overrides.get(
            extension,
            _LANGUAGE_ID_BY_EXTENSION.get(extension, extension.lstrip(".") or "plaintext"),
        )

    def _spec_for(self, path: str) -> _ServerSpec | None:
        extension = Path(path).suffix.lower()
        return next((spec for spec in _SPECS if extension in spec.extensions), None)

    def _auto_profile(self, spec: _ServerSpec) -> LspServerProfile | None:
        for executable_name in spec.executables:
            executable = _resolve_executable(executable_name, self._root)
            if executable:
                workspace_configuration: dict[str, object] = {}
                initialization_options: dict[str, object] = {}
                if spec.key == "python":
                    workspace_configuration = {
                        "python": {
                            "analysis": {
                                "diagnosticMode": "workspace",
                                "indexing": True,
                                "autoSearchPaths": True,
                                "useLibraryCodeForTypes": True,
                            }
                        }
                    }
                elif spec.key == "typescript":
                    initialization_options = {"disableAutomaticTypingAcquisition": True}
                    workspace_configuration = {
                        "diagnostics": {"ignoredCodes": []},
                        "typescript": {},
                        "javascript": {},
                    }
                return LspServerProfile(
                    command=[executable, *spec.args],
                    name=spec.key,
                    version="auto",
                    initialization_options=initialization_options,
                    workspace_configuration=workspace_configuration,
                )
        return None

    def _safe_manual_profile(self) -> LspServerProfile | None:
        profile = self._manual_profile
        if profile is None:
            return None
        executable = _resolve_executable(profile.command[0], self._root)
        if executable is None:
            return None
        return LspServerProfile(
            command=[executable, *profile.command[1:]],
            name=profile.name,
            version=profile.version,
            initialization_options=profile.initialization_options,
            workspace_configuration=profile.workspace_configuration,
        )

    async def _provider(self, path: str) -> tuple[ManagedLspClient, LspProvider] | None:
        spec = self._spec_for(path)
        profile = self._auto_profile(spec) if spec is not None else None
        key = spec.key if profile is not None and spec is not None else "manual"
        if profile is None:
            profile = self._safe_manual_profile()
        if profile is None or key in self._failures:
            return None
        existing = self._sessions.get(key)
        if existing is not None and existing[0].is_running:
            return existing
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing[0].is_running:
                return existing
            client = ManagedLspClient(profile, self._root)
            try:
                await client.start()
            except LspClientError:
                self._failures.add(key)
                return None
            session = (client, LspProvider(client, self._root))
            self._sessions[key] = session
            return session

    @asynccontextmanager
    async def document_provider(self, path: str) -> AsyncIterator[LspProvider | None]:
        """Synchronize one verified disk document and keep a bounded warm overlay."""
        session = await self._provider(path)
        if session is None:
            yield None
            return
        client, provider = session
        absolute = self._root / path
        try:
            text = read_source_text(self._root, path)
        except AgentError as exc:
            logger.debug("Cannot open analysis document (%s)", exc.code)
            yield None
            return
        uri = absolute.as_uri()
        await client.ensure_document(uri, self.language_id(path), text)
        await client.trim_documents(uri)
        yield provider

    async def notify_changes(
        self,
        added: Iterable[str],
        modified: Iterable[str],
        deleted: Iterable[str],
    ) -> None:
        if not self._sessions:
            return
        changes: list[lsp.FileEvent] = []
        for rel, change_type in (
            *((value, lsp.FileChangeType.Created) for value in added),
            *((value, lsp.FileChangeType.Changed) for value in modified),
            *((value, lsp.FileChangeType.Deleted) for value in deleted),
        ):
            changes.append(lsp.FileEvent(uri=(self._root / rel).as_uri(), type=change_type))
        if not changes:
            return
        # Every change must be delivered; the client batches the complete list.
        # A failure is propagated so no new snapshot is falsely marked ready.
        await asyncio.gather(
            *(
                client.notify_did_change_watched_files(changes)
                for client, _ in self._sessions.values()
            )
        )

    async def workspace_providers(self, paths: Iterable[str]) -> list[LspProvider]:
        """Start at most one session per distinct source extension/provider family."""
        representatives: dict[str, str] = {}
        for path in sorted(paths):
            extension = Path(path).suffix.lower()
            if extension:
                representatives.setdefault(extension, path)
        providers: list[LspProvider] = []
        seen: set[int] = set()
        for path in representatives.values():
            session = await self._provider(path)
            if session is None:
                continue
            provider = session[1]
            identity = id(provider)
            if identity in seen:
                continue
            seen.add(identity)
            providers.append(provider)
        return providers

    async def restart_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._failures.clear()
        await asyncio.gather(*(client.shutdown() for client, _ in sessions), return_exceptions=True)

    async def shutdown(self) -> None:
        await self.restart_all()
