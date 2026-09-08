from __future__ import annotations

import asyncio
import dataclasses
import getpass
import hashlib
import json
import queue
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

from .approvals import ApprovalDecision, ApprovalRequest
from .credentials import DataProtector, DpapiProtector
from .errors import AgentError

MAX_IPC_MESSAGE = 1_048_576


class IpcSecretStore:
    def __init__(self, path: Path, protector: DataProtector | None = None) -> None:
        self.path = path
        self.protector = protector or DpapiProtector()

    def load_or_create(self) -> bytes:
        if self.path.exists():
            value = self.protector.unprotect(self.path.read_bytes())
            if len(value) != 32:
                raise AgentError("ipc_unavailable", "IPC authentication key is invalid")
            return value
        value = secrets.token_bytes(32)
        protected = self.protector.protect(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("xb") as stream:
                stream.write(protected)
        except FileExistsError:
            return self.load_or_create()
        return value


def default_pipe_name() -> str:
    user = getpass.getuser().encode("utf-8")
    suffix = hashlib.sha256(user).hexdigest()[:16]
    return rf"\\.\pipe\Codito.Agent.{suffix}"


RequestHandler = Callable[[dict[str, Any]], dict[str, Any]]


class NamedPipeServer:
    """Authenticated JSON-over-AF_PIPE endpoint for the tray companion.

    `recv_bytes` is used deliberately; remote data is never unpickled.
    """

    def __init__(self, address: str, authkey: bytes, handler: RequestHandler) -> None:
        self.address = address
        self.authkey = authkey
        self.handler = handler
        self._stop = threading.Event()
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._serve, name="codito-ipc", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._listener = Listener(self.address, family="AF_PIPE", authkey=self.authkey)
        while not self._stop.is_set():
            try:
                connection = self._listener.accept()
            except (OSError, EOFError):
                break
            with connection:
                try:
                    raw = connection.recv_bytes(MAX_IPC_MESSAGE)
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                    response = self.handler(request)
                except Exception:
                    response = {"ok": False, "error": "invalid_ipc_request"}
                connection.send_bytes(
                    json.dumps(response, separators=(",", ":"), default=str).encode()
                )

    def close(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)


class NamedPipeClient:
    def __init__(self, address: str, authkey: bytes) -> None:
        self.address = address
        self.authkey = authkey

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        with Client(self.address, family="AF_PIPE", authkey=self.authkey) as connection:
            connection.send_bytes(json.dumps(request, separators=(",", ":")).encode())
            response = json.loads(connection.recv_bytes(MAX_IPC_MESSAGE))
        if not isinstance(response, dict):
            raise AgentError("ipc_unavailable", "Daemon returned a malformed IPC response")
        return response


@dataclass(slots=True)
class _PendingApproval:
    request: ApprovalRequest
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[ApprovalDecision]


class QueuedApprovalPrompt:
    """Bridge async daemon approvals to a persistent tray-owned dialog queue."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = threading.Lock()

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        with self._lock:
            self._pending[request.request_id] = _PendingApproval(request, loop, future)
        self._queue.put(request.request_id)
        try:
            return await future
        finally:
            with self._lock:
                self._pending.pop(request.request_id, None)

    def next_request(self) -> dict[str, Any] | None:
        try:
            request_id = self._queue.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return None
        value = dataclasses.asdict(pending.request)
        value["risk"] = pending.request.risk.value
        return value

    def respond(self, request_id: str, decision: str) -> bool:
        try:
            parsed = ApprovalDecision(decision)
        except ValueError:
            return False
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.loop.call_soon_threadsafe(pending.future.set_result, parsed)
        return True

    def deny_all(self) -> None:
        with self._lock:
            values = list(self._pending.values())
        for pending in values:
            if not pending.future.done():
                pending.loop.call_soon_threadsafe(pending.future.set_result, ApprovalDecision.DENY)
