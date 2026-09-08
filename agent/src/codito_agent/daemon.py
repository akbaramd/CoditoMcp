from __future__ import annotations

import asyncio
import platform
from datetime import datetime
from pathlib import Path
from typing import Any

from .approvals import ApprovalManager
from .broker_client import BrokerClient
from .config import AgentConfig
from .credentials import DeviceCredentialStore
from .db import AgentDatabase
from .errors import AgentError
from .ipc import IpcSecretStore, NamedPipeServer, QueuedApprovalPrompt, default_pipe_name
from .models import ProjectMode
from .operation_gate import ProjectOperationGate
from .patching import PatchService
from .paths import ProjectPathResolver
from .protocol_adapter import AgentProtocolAdapter
from .read_tools import ProjectReadService, ToolResponse
from .relay_client import RelayClient, RelayTokenStore
from .shell import ShellManager
from .websocket_client import DeviceWebSocketClient


class CoditoDaemon:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        config.ensure_directories()
        self.database = AgentDatabase(config.data_directory / "agent.sqlite3")
        self.credentials = DeviceCredentialStore(config.data_directory / "credentials")
        self.token_store = RelayTokenStore(config.data_directory / "tokens.dpapi")
        state = self.token_store.load()
        if state.device_id is None or state.link_id is None:
            raise AgentError("enrollment_required", "Complete desktop login and enrollment first")
        self.relay = RelayClient(
            config.relay_http_url,
            config.desktop_client_id,
            self.token_store,
            self.credentials,
        )
        self.approval_queue = QueuedApprovalPrompt()
        self.approvals = ApprovalManager(self.approval_queue)
        self.resolver = ProjectPathResolver()
        self.operation_gate = ProjectOperationGate()
        self.broker = BrokerClient(config.broker_path)
        self.reads = ProjectReadService(self.database, self.resolver)
        self.patches = PatchService(
            self.database,
            config.data_directory / "journals",
            self.resolver,
            self.operation_gate,
        )
        self.shells = ShellManager(
            self.database,
            self.resolver,
            self.broker,
            self.approvals,
            config.data_directory,
            account_id=state.account_id,
            device_id=state.device_id,
            operation_gate=self.operation_gate,
        )
        self.adapter = AgentProtocolAdapter(
            self.database,
            self.reads,
            self.patches,
            self.shells,
            device_id=state.device_id,
            account_id=state.account_id,
            approvals=self.approvals,
            read_concurrency=config.read_concurrency,
        )
        self.websocket = DeviceWebSocketClient(
            url=config.relay_websocket_url,
            database=self.database,
            credentials=self.credentials,
            ticket_provider=self.relay.issue_websocket_ticket,
            operation_handler=self._execute_operation,
            project_metadata=lambda: [
                project.public_dict() for project in self.database.list_projects()
            ],
            max_pending=config.max_pending_operations,
            connection_callback=self._connection_changed,
        )
        self._online = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._grant_clear_task: asyncio.Task[None] | None = None
        self._metadata_sync_task: asyncio.Task[None] | None = None
        ipc_key = IpcSecretStore(config.data_directory / "ipc-key.dpapi").load_or_create()
        self.ipc = NamedPipeServer(default_pipe_name(), ipc_key, self._handle_ipc)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        recovered = await asyncio.to_thread(self.patches.recover)
        del recovered
        self.ipc.start()
        websocket_task = asyncio.create_task(self.websocket.run(), name="codito-websocket")
        try:
            await self._stop.wait()
        finally:
            await self.websocket.stop()
            websocket_task.cancel()
            tasks = [websocket_task]
            if self._metadata_sync_task is not None:
                self._metadata_sync_task.cancel()
                tasks.append(self._metadata_sync_task)
            await asyncio.gather(*tasks, return_exceptions=True)
            self.approval_queue.deny_all()
            self.approvals.clear("shutdown")
            self.ipc.close()

    async def _execute_operation(
        self,
        tool_name: str,
        payload: dict[str, Any],
        grant_id: str,
        link_id: str,
        epoch: int,
        deadline_at: datetime,
        reconcile_duplicate: bool,
    ) -> ToolResponse:
        return await self.adapter.execute(
            tool_name,
            payload,
            grant_id=grant_id,
            link_id=link_id,
            connection_epoch=epoch,
            deadline_at=deadline_at,
            reconcile_duplicate=reconcile_duplicate,
        )

    def _connection_changed(self, connected: bool) -> None:
        self._online = connected
        self.adapter.online = connected
        if connected:
            self.shells.reconnected()
            if self._grant_clear_task is not None:
                self._grant_clear_task.cancel()
                self._grant_clear_task = None
        else:
            self.shells.disconnected()
            # A decision made in a dialog opened for a previous socket fence
            # must never authorize work after reconnect/revocation.
            self.approvals.invalidate_pending("relay_disconnect")
            self.approval_queue.deny_all()
            if self._loop is not None and (
                self._grant_clear_task is None or self._grant_clear_task.done()
            ):
                self._grant_clear_task = self._loop.create_task(
                    self._clear_grants_after_disconnect()
                )

    async def _clear_grants_after_disconnect(self) -> None:
        try:
            await asyncio.sleep(60)
            if not self._online:
                self.approvals.clear("relay_disconnect")
        except asyncio.CancelledError:
            return

    def _project_metadata_changed(self) -> None:
        if self._loop is None or not self._online:
            return

        def schedule() -> None:
            if self._metadata_sync_task is None or self._metadata_sync_task.done():
                self._metadata_sync_task = asyncio.create_task(
                    self.websocket.sync_projects(), name="codito-project-sync"
                )

        self._loop.call_soon_threadsafe(schedule)

    def _handle_ipc(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "status":
            state = self.token_store.load()
            return {
                "ok": True,
                "online": self._online,
                "account_id": state.account_id,
                "device_name": platform.node() or "Windows device",
                "device_id": state.device_id,
                "link_id": state.link_id,
                "mcp_url": state.mcp_url,
                "connection_epoch": self.websocket.connection_epoch,
                "last_disconnect_reason": self.websocket.last_disconnect_reason,
                "pending_approvals": self.approval_queue.pending_count,
                "projects": [
                    {**project.public_dict(), "root": str(project.root)}
                    for project in self.database.list_projects(enabled_only=False)
                ],
            }
        if action == "approval.next":
            return {"ok": True, "approval": self.approval_queue.next_request()}
        if action == "activity.list":
            limit = int(request.get("limit", 50))
            return {"ok": True, "activity": self.database.list_recent_operations(limit)}
        if action == "approval.respond":
            accepted = self.approval_queue.respond(
                str(request.get("request_id", "")), str(request.get("decision", ""))
            )
            return {"ok": accepted}
        if action == "project.register":
            project = self.database.register_project(
                str(request.get("title", "")), Path(str(request.get("path", "")))
            )
            self._project_metadata_changed()
            return {"ok": True, "project": {**project.public_dict(), "root": str(project.root)}}
        if action == "project.mode":
            mode = ProjectMode(str(request.get("mode", "")))
            if (
                mode is ProjectMode.NATIVE_TRUSTED
                and request.get("acknowledge_full_user_authority") is not True
            ):
                raise AgentError(
                    "confirmation_required",
                    "Native trusted mode requires acknowledging full user authority",
                )
            self.database.set_project_mode(str(request.get("project_id", "")), mode)
            self.approvals.clear("trust_change")
            self._project_metadata_changed()
            return {"ok": True}
        if action == "shutdown" and self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
            return {"ok": True}
        raise AgentError("invalid_ipc_request", "Unsupported local IPC action")
