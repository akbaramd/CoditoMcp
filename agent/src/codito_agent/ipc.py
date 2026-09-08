from __future__ import annotations

import asyncio
import dataclasses
import getpass
import hashlib
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

from .approvals import ApprovalDecision, ApprovalRequest
from .credentials import DataProtector, DpapiProtector
from .diagnostics import event
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
                except AgentError as exc:
                    response = {
                        "ok": False,
                        "error": exc.code,
                        "message": exc.message,
                    }
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
    displayed: bool = False
    review_requested: bool = False
    toast_tokens: dict[str, str] = field(default_factory=dict)


class QueuedApprovalPrompt:
    """Bridge async daemon approvals to a persistent tray-owned dialog queue."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = threading.Lock()

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        with self._lock:
            decisions = ["deny", "allow_once", "review"]
            if request.persistent_read_eligible:
                decisions.append("allow_always_read")
            if request.persistent_shell_eligible:
                decisions.append("allow_always_shell")
            if request.persistent_screen_eligible:
                decisions.append("allow_always_screen")
            pending = _PendingApproval(request, loop, future)
            pending.toast_tokens = {decision: secrets.token_urlsafe(32) for decision in decisions}
            self._pending[request.request_id] = pending
        event("approval_created", request.request_id)
        try:
            return await future
        finally:
            event("approval_closed", request.request_id)
            with self._lock:
                self._pending.pop(request.request_id, None)

    def next_request(self, request_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            # Peek, never consume. A tray crash/IPC error must not lose a prompt.
            pending = next(
                (
                    p
                    for p in self._pending.values()
                    if not p.future.done()
                    and (request_id is None or p.request.request_id == request_id)
                ),
                None,
            )
        if pending is None:
            return None
        value = dataclasses.asdict(pending.request)
        value["risk"] = pending.request.risk.value
        value["toast_tokens"] = dict(pending.toast_tokens)
        return value

    def respond_toast(self, request_id: str, decision: str, token: str) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if (
                pending is None
                or pending.future.done()
                or pending.request.deadline_at <= datetime.now(UTC)
            ):
                return False
            expected = pending.toast_tokens.get(decision)
            if expected is None or not secrets.compare_digest(expected, token):
                return False
            if decision == "review":
                pending.review_requested = True
                return True
            # Consume every button token together; duplicate activations cannot change a choice.
            pending.toast_tokens.clear()
        return self.respond(request_id, decision)

    def consume_review_request(self) -> str | None:
        with self._lock:
            for pending in self._pending.values():
                if (
                    pending.review_requested
                    and not pending.future.done()
                    and pending.request.deadline_at > datetime.now(UTC)
                ):
                    pending.review_requested = False
                    return pending.request.request_id
        return None

    def is_pending(self, request_id: str) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            return pending is not None and not pending.future.done()

    def mark_displayed(self, request_id: str) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.future.done():
                return False
            if not pending.displayed:
                pending.displayed = True
                event("approval_displayed", request_id)
            return True

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def respond(self, request_id: str, decision: str) -> bool:
        try:
            parsed = ApprovalDecision(decision)
        except ValueError:
            return False
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.loop.call_soon_threadsafe(self._resolve, pending, parsed)
        return True

    @staticmethod
    def _resolve(pending: _PendingApproval, decision: ApprovalDecision) -> None:
        # Expiry, disconnect and a UI click can race before the event-loop callback.
        if not pending.future.done():
            event("approval_decision", pending.request.request_id, decision.value)
            pending.future.set_result(decision)

    def deny_all(self) -> None:
        with self._lock:
            values = list(self._pending.values())
        for pending in values:
            if not pending.future.done():
                pending.loop.call_soon_threadsafe(self._resolve, pending, ApprovalDecision.DENY)
