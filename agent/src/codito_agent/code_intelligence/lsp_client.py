"""Bounded LSP client wrapping pygls BaseLanguageClient.

Design:
- start() launches the server subprocess, initializes, and returns.
- shutdown() sends shutdown+exit and terminates the process with a hard timeout.
- All requests use _request_with_timeout() which wraps async calls with asyncio.wait_for.
- No interactive shell wrappers; server is a direct stdio subprocess.
- workspace/applyEdit and executeCommand from the server are rejected silently.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.lsp.client import BaseLanguageClient  # type: ignore[attr-defined]

from .owned_process import OwnedProcessTree
from .types import CapabilitySet

logger = logging.getLogger(__name__)
_wire_logger = logging.getLogger("pygls")
_wire_logger.handlers = [logging.NullHandler()]
_wire_logger.propagate = False

_INIT_TIMEOUT_S = 30.0
_REQUEST_TIMEOUT_S = 30.0
_SHUTDOWN_TIMEOUT_S = 5.0


def _canonical_document_uri(uri: str) -> str:
    """Build a stable comparison key while preserving the original LSP URI."""
    parsed = urlparse(uri)
    if parsed.scheme.casefold() != "file" or parsed.query or parsed.fragment:
        return uri
    path = unquote(parsed.path)
    if os.name == "nt":
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            path = f"//{parsed.netloc}{path}"
    return os.path.normcase(os.path.normpath(path))


class _Codito_LSPClient(BaseLanguageClient):
    """Pinned pygls client; report only categorical protocol failures."""

    def report_server_error(self, error: Exception, source: Any) -> None:
        for future in self.protocol._request_futures.values():
            if not future.done():
                future.set_exception(LspClientError("Language-server protocol failure"))

    async def server_exit(self, server: asyncio.subprocess.Process) -> None:
        logger.debug("Language-server process exited")


class LspClientError(Exception):
    pass


class LspServerProfile:
    """Configuration for a trusted locally-installed LSP server."""

    def __init__(
        self,
        command: list[str],
        name: str = "lsp-server",
        version: str = "0.0.0",
        initialization_options: dict[str, Any] | None = None,
        workspace_configuration: dict[str, Any] | None = None,
    ) -> None:
        if not command:
            raise ValueError("LSP server command must be non-empty")
        self.command = command
        self.name = name
        self.version = version
        self.initialization_options = initialization_options or {}
        self.workspace_configuration = workspace_configuration or {}


class ManagedLspClient:
    """Lifecycle wrapper around a pygls BaseLanguageClient + server subprocess."""

    def __init__(self, profile: LspServerProfile, workspace_root: Path) -> None:
        self._profile = profile
        self._workspace_root = workspace_root
        self._client: _Codito_LSPClient | None = None
        self._capabilities: CapabilitySet | None = None
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tree: OwnedProcessTree | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._documents: dict[str, tuple[int, str, str]] = {}
        self._document_aliases: dict[str, str] = {}
        self._push_diagnostics: dict[str, lsp.PublishDiagnosticsParams] = {}
        self._diagnostic_events: dict[str, asyncio.Event] = {}
        self._pull_diagnostics: dict[str, tuple[str | None, list[lsp.Diagnostic]]] = {}
        self._sync_kind = lsp.TextDocumentSyncKind.Full
        self._open_close = True
        self._save_enabled = False
        self._active_progress: set[int | str] = set()
        self._progress_idle = asyncio.Event()
        self._progress_idle.set()
        self._progress_epoch = 0

    @property
    def capabilities(self) -> CapabilitySet:
        if self._capabilities is None:
            return CapabilitySet()
        return self._capabilities

    @property
    def is_running(self) -> bool:
        return bool(
            self._started
            and self._client is not None
            and self._client._server is not None
            and self._client._server.returncode is None
        )

    async def start(self) -> None:
        if self.is_running:
            return
        await self._force_stop()
        self._loop = asyncio.get_running_loop()
        # Runtime logging setup may occur after module import; prevent wire payload logging.
        for namespace in (
            "pygls.protocol.json_rpc",
            "pygls.protocol.language_server",
            "pygls.client",
            "pygls.feature_manager",
        ):
            logging.getLogger(namespace).disabled = True
        client = _Codito_LSPClient(name=self._profile.name, version=self._profile.version)
        self._client = client
        self._register_handlers(client)

        cmd = self._profile.command
        try:
            # pygls 2.x BaseLanguageClient.start_io is itself async: it launches
            # the child language server and returns once stdio is connected.
            await asyncio.wait_for(
                client.start_io(
                    cmd[0],
                    *cmd[1:],
                    cwd=str(self._workspace_root),
                    **OwnedProcessTree.spawn_options(),
                ),
                timeout=_INIT_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await self._force_stop()
            raise LspClientError("LSP server did not connect within timeout") from exc
        except Exception as exc:
            await self._force_stop()
            raise LspClientError("LSP server failed to start") from exc

        process = client._server
        if process is None:
            await self._force_stop()
            raise LspClientError("Language-server process is unavailable")
        try:
            self._tree = OwnedProcessTree(process)
        except OSError as exc:
            await self._force_stop()
            raise LspClientError("Provider lifetime containment failed") from exc
        if process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))

        # Send initialize
        root_uri = self._workspace_root.as_uri()
        init_params = lsp.InitializeParams(
            capabilities=lsp.ClientCapabilities(
                general=lsp.GeneralClientCapabilities(
                    position_encodings=[lsp.PositionEncodingKind.Utf16]
                ),
                text_document=lsp.TextDocumentClientCapabilities(
                    synchronization=lsp.TextDocumentSyncClientCapabilities(
                        dynamic_registration=False,
                        did_save=True,
                        will_save=False,
                    ),
                    hover=lsp.HoverClientCapabilities(
                        content_format=[lsp.MarkupKind.Markdown, lsp.MarkupKind.PlainText],
                    ),
                    definition=lsp.DefinitionClientCapabilities(dynamic_registration=False),
                    references=lsp.ReferenceClientCapabilities(dynamic_registration=False),
                    implementation=lsp.ImplementationClientCapabilities(dynamic_registration=False),
                    document_symbol=lsp.DocumentSymbolClientCapabilities(
                        dynamic_registration=False,
                        hierarchical_document_symbol_support=True,
                    ),
                    publish_diagnostics=lsp.PublishDiagnosticsClientCapabilities(
                        version_support=True,
                        related_information=True,
                        code_description_support=True,
                    ),
                    call_hierarchy=lsp.CallHierarchyClientCapabilities(dynamic_registration=False),
                    type_hierarchy=lsp.TypeHierarchyClientCapabilities(dynamic_registration=False),
                    diagnostic=lsp.DiagnosticClientCapabilities(dynamic_registration=False),
                ),
                workspace=lsp.WorkspaceClientCapabilities(
                    apply_edit=False,
                    configuration=True,
                    did_change_configuration=lsp.DidChangeConfigurationClientCapabilities(
                        dynamic_registration=False
                    ),
                    symbol=lsp.WorkspaceSymbolClientCapabilities(dynamic_registration=False),
                    workspace_folders=True,
                    did_change_watched_files=lsp.DidChangeWatchedFilesClientCapabilities(
                        dynamic_registration=False,
                        relative_pattern_support=False,
                    ),
                ),
                window=lsp.WindowClientCapabilities(work_done_progress=True),
            ),
            root_uri=root_uri,
            workspace_folders=[lsp.WorkspaceFolder(uri=root_uri, name=self._workspace_root.name)],
            initialization_options=self._profile.initialization_options or None,
        )

        try:
            result = await asyncio.wait_for(
                client.initialize_async(init_params), timeout=_INIT_TIMEOUT_S
            )
        except TimeoutError as exc:
            await self._force_stop()
            raise LspClientError("LSP initialize timed out") from exc
        except Exception as exc:
            await self._force_stop()
            raise LspClientError("LSP initialize failed") from exc

        if result is None or str(result.capabilities.position_encoding or "utf-16") != "utf-16":
            await self._force_stop()
            raise LspClientError("Language server did not agree to UTF-16 positions")
        self._capabilities = CapabilitySet.from_lsp_capabilities(result.capabilities)
        sync = result.capabilities.text_document_sync
        if isinstance(sync, lsp.TextDocumentSyncOptions):
            self._open_close = sync.open_close is not False
            self._sync_kind = sync.change or lsp.TextDocumentSyncKind.None_
            self._save_enabled = bool(sync.save)
        elif isinstance(sync, lsp.TextDocumentSyncKind):
            self._sync_kind = sync
        client.initialized(lsp.InitializedParams())
        client.workspace_did_change_configuration(
            lsp.DidChangeConfigurationParams(settings=self._profile.workspace_configuration)
        )
        self._started = True

    def _register_handlers(self, client: _Codito_LSPClient) -> None:
        def publish_diagnostics(params: lsp.PublishDiagnosticsParams) -> None:
            document_uri = self._document_aliases.get(_canonical_document_uri(params.uri))
            if document_uri is None or document_uri not in self._documents:
                return
            self._push_diagnostics[document_uri] = params
            self._diagnostic_events.setdefault(document_uri, asyncio.Event()).set()
            if self._capabilities is not None:
                self._capabilities.diagnostics_push = True

        def workspace_configuration(params: lsp.ConfigurationParams) -> list[Any]:
            configured = self._profile.workspace_configuration
            results: list[Any] = []
            for item in params.items:
                value: Any = configured
                for part in (item.section or "").split("."):
                    if not part:
                        continue
                    value = value.get(part, {}) if isinstance(value, dict) else {}
                results.append(value)
            return results

        def reject_workspace_edit(
            params: lsp.ApplyWorkspaceEditParams,
        ) -> lsp.ApplyWorkspaceEditResult:
            del params
            return lsp.ApplyWorkspaceEditResult(
                applied=False,
                failure_reason="Code Intelligence is read-only; use file_patch for mutations",
            )

        def accept_progress_token(params: lsp.WorkDoneProgressCreateParams) -> None:
            self._progress_epoch += 1
            if params.token not in self._active_progress:
                self._progress_idle.set()

        def ignore_progress(params: lsp.ProgressParams) -> None:
            value = params.value
            kind = getattr(value, "kind", None)
            if kind is None and isinstance(value, dict):
                kind = value.get("kind")
            self._progress_epoch += 1
            if kind == "begin":
                self._active_progress.add(params.token)
                self._progress_idle.clear()
            elif kind == "end":
                self._active_progress.discard(params.token)
                if not self._active_progress:
                    self._progress_idle.set()

        handlers: tuple[tuple[str, Any], ...] = (
            (lsp.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS, publish_diagnostics),
            (lsp.WORKSPACE_CONFIGURATION, workspace_configuration),
            (lsp.WORKSPACE_APPLY_EDIT, reject_workspace_edit),
            (lsp.WINDOW_WORK_DONE_PROGRESS_CREATE, accept_progress_token),
            (lsp.PROGRESS, ignore_progress),
        )
        for method, handler in handlers:
            client.feature(method)(handler)

    async def wait_for_analysis_ready(
        self, timeout_seconds: float = 8.0, quiet_seconds: float = 0.4
    ) -> bool:
        """Wait until provider work-done progress is stably idle."""
        if not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        epoch = self._progress_epoch
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if self._active_progress:
                try:
                    await asyncio.wait_for(self._progress_idle.wait(), remaining)
                except TimeoutError:
                    return False
                epoch = self._progress_epoch
                continue
            await asyncio.sleep(min(quiet_seconds, remaining))
            if not self._active_progress and self._progress_epoch == epoch:
                return True
            epoch = self._progress_epoch

    async def ensure_document(self, uri: str, language_id: str, text: str) -> int:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        uri_key = _canonical_document_uri(uri)
        uri = self._document_aliases.get(uri_key, uri)
        existing = self._documents.get(uri)
        if existing is None:
            version = 1
            self._document_aliases[uri_key] = uri
            self._documents[uri] = (version, language_id, text)
            self._diagnostic_events[uri] = asyncio.Event()
            if self._open_close:
                self._client.text_document_did_open(
                    lsp.DidOpenTextDocumentParams(
                        text_document=lsp.TextDocumentItem(
                            uri=uri, language_id=language_id, version=version, text=text
                        )
                    )
                )
            return version
        version, previous_language, old_text = existing
        if old_text == text:
            return version
        version += 1
        self._documents[uri] = (version, previous_language or language_id, text)
        self._push_diagnostics.pop(uri, None)
        self._pull_diagnostics.pop(uri, None)
        self._diagnostic_events[uri] = asyncio.Event()
        if self._sync_kind == lsp.TextDocumentSyncKind.Incremental:
            old_lines = old_text.split("\n")
            change: (
                lsp.TextDocumentContentChangePartial | lsp.TextDocumentContentChangeWholeDocument
            ) = lsp.TextDocumentContentChangePartial(
                range=lsp.Range(
                    start=lsp.Position(line=0, character=0),
                    end=lsp.Position(
                        line=len(old_lines) - 1,
                        character=len(old_lines[-1].encode("utf-16-le")) // 2,
                    ),
                ),
                text=text,
            )
        else:
            change = lsp.TextDocumentContentChangeWholeDocument(text=text)
        if self._sync_kind == lsp.TextDocumentSyncKind.None_:
            if self._open_close:
                self._client.text_document_did_close(
                    lsp.DidCloseTextDocumentParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri)
                    )
                )
                self._client.text_document_did_open(
                    lsp.DidOpenTextDocumentParams(
                        text_document=lsp.TextDocumentItem(
                            uri=uri, language_id=language_id, version=version, text=text
                        )
                    )
                )
        else:
            self._client.text_document_did_change(
                lsp.DidChangeTextDocumentParams(
                    text_document=lsp.VersionedTextDocumentIdentifier(uri=uri, version=version),
                    content_changes=[change],
                )
            )
            if self._save_enabled:
                self._client.text_document_did_save(
                    lsp.DidSaveTextDocumentParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri), text=text
                    )
                )
        return version

    async def trim_documents(self, keep_uri: str, max_open: int = 128) -> None:
        while len(self._documents) > max_open:
            oldest = next((uri for uri in self._documents if uri != keep_uri), None)
            if oldest is None:
                return
            await self.notify_did_close(oldest)

    async def diagnostic_snapshot(self, uri: str) -> tuple[list[lsp.Diagnostic], str]:
        uri = self._document_aliases.get(_canonical_document_uri(uri), uri)
        if self.capabilities.diagnostics_pull:
            previous = self._pull_diagnostics.get(uri)
            report = await self.request_diagnostic(uri, previous[0] if previous else None)
            if isinstance(
                report,
                (lsp.FullDocumentDiagnosticReport, lsp.RelatedFullDocumentDiagnosticReport),
            ):
                diagnostics = list(report.items)
                self._pull_diagnostics[uri] = (report.result_id, diagnostics)
                return diagnostics, "ready"
            if isinstance(
                report,
                (
                    lsp.UnchangedDocumentDiagnosticReport,
                    lsp.RelatedUnchangedDocumentDiagnosticReport,
                ),
            ):
                return (list(previous[1]), "ready") if previous is not None else ([], "pending")
            return [], "unavailable"

        event = self._diagnostic_events.setdefault(uri, asyncio.Event())
        if uri not in self._push_diagnostics:
            try:
                await asyncio.wait_for(event.wait(), 1.5)
            except TimeoutError:
                return [], "pending"
        push_report = self._push_diagnostics.get(uri)
        document = self._documents.get(uri)
        if push_report is None or document is None:
            return [], "pending"
        if push_report.version is None:
            return list(push_report.diagnostics), "partial"
        if push_report.version != document[0]:
            return [], "stale"
        return list(push_report.diagnostics), "ready"

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while await stream.read(65536):
            pass  # Do not persist potentially sensitive provider stderr.

    async def shutdown(self) -> None:
        client = self._client
        try:
            if self.is_running and client is not None:
                try:
                    await asyncio.wait_for(client.shutdown_async(None), timeout=_SHUTDOWN_TIMEOUT_S)
                    client.exit(None)
                except Exception:
                    logger.debug("Provider graceful shutdown did not finish")
        finally:
            await asyncio.shield(self._force_stop())

    async def _force_stop(self) -> None:
        client, tree = self._client, self._tree
        self._client = None
        self._tree = None
        self._started = False
        self._documents.clear()
        self._document_aliases.clear()
        self._push_diagnostics.clear()
        self._diagnostic_events.clear()
        self._pull_diagnostics.clear()
        self._active_progress.clear()
        self._progress_idle.set()
        self._progress_epoch = 0
        if tree is not None:
            await tree.stop()
        elif client is not None and client._server is not None:
            process = client._server
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 3)
        if client is not None:
            try:
                await asyncio.wait_for(client.stop(), 3)  # type: ignore[no-untyped-call]
            except TimeoutError:
                logger.debug("Provider transport cleanup reached its bound")
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def notify_did_open(self, uri: str, language_id: str, text: str, version: int) -> None:
        if version != 1 and uri not in self._documents:
            raise LspClientError("First language-server document version must be 1")
        await self.ensure_document(uri, language_id, text)

    async def notify_did_close(self, uri: str) -> None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        uri_key = _canonical_document_uri(uri)
        uri = self._document_aliases.pop(uri_key, uri)
        for alias, target in list(self._document_aliases.items()):
            if target == uri:
                self._document_aliases.pop(alias, None)
        self._documents.pop(uri, None)
        self._push_diagnostics.pop(uri, None)
        self._diagnostic_events.pop(uri, None)
        self._pull_diagnostics.pop(uri, None)
        if self._open_close:
            self._client.text_document_did_close(
                lsp.DidCloseTextDocumentParams(text_document=lsp.TextDocumentIdentifier(uri=uri))
            )

    async def notify_did_change_watched_files(self, changes: list[lsp.FileEvent]) -> None:
        if self._client is None:
            return
        for offset in range(0, len(changes), 256):
            self._client.workspace_did_change_watched_files(
                lsp.DidChangeWatchedFilesParams(changes=changes[offset : offset + 256])
            )

    async def request_definition(
        self, uri: str, line: int, character: int
    ) -> Sequence[lsp.Location] | Sequence[lsp.LocationLink] | lsp.Location | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.text_document_definition_async(
                    lsp.DefinitionParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri),
                        position=lsp.Position(line=line, character=character),
                    )
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_references(
        self, uri: str, line: int, character: int, include_declaration: bool
    ) -> Sequence[lsp.Location] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.text_document_references_async(
                    lsp.ReferenceParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri),
                        position=lsp.Position(line=line, character=character),
                        context=lsp.ReferenceContext(include_declaration=include_declaration),
                    )
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_implementation(
        self, uri: str, line: int, character: int
    ) -> Sequence[lsp.Location] | Sequence[lsp.LocationLink] | lsp.Location | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.text_document_implementation_async(
                    lsp.ImplementationParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri),
                        position=lsp.Position(line=line, character=character),
                    )
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_hover(self, uri: str, line: int, character: int) -> lsp.Hover | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.text_document_hover_async(
                    lsp.HoverParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri),
                        position=lsp.Position(line=line, character=character),
                    )
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_document_symbols(
        self, uri: str
    ) -> Sequence[lsp.SymbolInformation] | Sequence[lsp.DocumentSymbol] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.text_document_document_symbol_async(
                    lsp.DocumentSymbolParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri),
                    )
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_workspace_symbols(
        self, query: str
    ) -> Sequence[lsp.SymbolInformation] | Sequence[lsp.WorkspaceSymbol] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.workspace_symbol_async(lsp.WorkspaceSymbolParams(query=query)),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_diagnostic(
        self, uri: str, previous_result_id: str | None = None
    ) -> lsp.DocumentDiagnosticReport | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.text_document_diagnostic_async(
                    lsp.DocumentDiagnosticParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri),
                        previous_result_id=previous_result_id,
                    )
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_prepare_call_hierarchy(
        self, uri: str, line: int, character: int
    ) -> Sequence[lsp.CallHierarchyItem] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.text_document_prepare_call_hierarchy_async(
                    lsp.CallHierarchyPrepareParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri),
                        position=lsp.Position(line=line, character=character),
                    )
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_incoming_calls(
        self, item: lsp.CallHierarchyItem
    ) -> Sequence[lsp.CallHierarchyIncomingCall] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.call_hierarchy_incoming_calls_async(
                    lsp.CallHierarchyIncomingCallsParams(item=item)
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_outgoing_calls(
        self, item: lsp.CallHierarchyItem
    ) -> Sequence[lsp.CallHierarchyOutgoingCall] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.call_hierarchy_outgoing_calls_async(
                    lsp.CallHierarchyOutgoingCallsParams(item=item)
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_prepare_type_hierarchy(
        self, uri: str, line: int, character: int
    ) -> Sequence[lsp.TypeHierarchyItem] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.text_document_prepare_type_hierarchy_async(
                    lsp.TypeHierarchyPrepareParams(
                        text_document=lsp.TextDocumentIdentifier(uri=uri),
                        position=lsp.Position(line=line, character=character),
                    )
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_supertypes(
        self, item: lsp.TypeHierarchyItem
    ) -> Sequence[lsp.TypeHierarchyItem] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.type_hierarchy_supertypes_async(
                    lsp.TypeHierarchySupertypesParams(item=item)
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc

    async def request_subtypes(
        self, item: lsp.TypeHierarchyItem
    ) -> Sequence[lsp.TypeHierarchyItem] | None:
        if self._client is None or not self.is_running:
            raise LspClientError("Language-server session is unavailable")
        try:
            return await asyncio.wait_for(
                self._client.type_hierarchy_subtypes_async(
                    lsp.TypeHierarchySubtypesParams(item=item)
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            await asyncio.shield(self._force_stop())
            raise LspClientError("Language-server request timed out") from exc
