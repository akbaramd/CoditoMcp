"""Device-scoped, locally approved reads. Never grants mutation authority."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any

from codito_protocol import DeviceReadInput, DeviceReadResult

from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .device_paths import WindowsReadScope
from .errors import AgentError
from .read_tools import ToolResponse, _detect_text, _newline_style


class DeviceReadService:
    def __init__(
        self,
        approvals: ApprovalManager,
        *,
        account_id: str,
        device_id: str,
        scope_factory: Callable[[str], WindowsReadScope] = WindowsReadScope,
    ) -> None:
        self.approvals = approvals
        self.account_id = account_id
        self.device_id = device_id
        self.scope_factory = scope_factory

    async def execute(
        self,
        request: DeviceReadInput,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        generation = self.approvals.generation
        cancelled = threading.Event()

        def check() -> None:
            if cancelled.is_set():
                raise AgentError("approval_expired", "The read request was cancelled")
            self.approvals.ensure_current(generation, deadline_at)

        check()
        try:
            # Only attribute/identity checks occur before consent. Pin the approved
            # directory until the data operation finishes, including cancellation.
            with self.scope_factory(request.scope_path) as scope:
                approval = self.approvals.build_request(
                    account_id=self.account_id,
                    grant_id=grant_id,
                    link_id=link_id,
                    device_id=self.device_id,
                    project_id="",
                    project_title="Outside projects",
                    capability="device:read",
                    action_digest=action_digest(request),
                    connection_epoch=connection_epoch,
                    deadline_at=deadline_at,
                    risk=ApprovalRisk.READ,
                    summary=(
                        f"Read outside projects: {request.operation}\n"
                        f"Scope: {request.scope_path}\nPath: {request.path or '(scope root)'}\n"
                        f"Purpose: {request.purpose}"
                    ),
                    requested_external_paths=(request.scope_path,),
                )
                await self.approvals.request(
                    approval,
                    session_eligible=False,
                    read_scope=(request.scope_path, scope.identity),
                )
                check()
                worker = asyncio.create_task(asyncio.to_thread(self._read, scope, request, check))
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # to_thread cannot be cancelled. Do not close pinned handles
                    # underneath it or return data after the request was cancelled.
                    cancelled.set()
                    while not worker.done():
                        # Cleanup drains errors without exposing content from a cancelled read.
                        with suppress(asyncio.CancelledError, Exception):
                            await asyncio.shield(worker)
                    if not worker.cancelled():
                        worker.exception()
                    raise
                check()
        except OSError as exc:
            raise AgentError("read_failed", "Windows denied access or the path changed") from exc
        validated = DeviceReadResult.model_validate(result)
        return ToolResponse(
            validated.model_dump(mode="json", exclude_none=True),
            "Approved device read completed"
            + (" (bounded/truncated)" if validated.truncated else ""),
        )

    @staticmethod
    def _read(
        scope: WindowsReadScope,
        request: DeviceReadInput,
        check: Callable[[], None],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "operation": request.operation,
            "scope_path": request.scope_path,
            "path": request.path,
        }
        if request.operation == "list_directory":
            entries: list[dict[str, Any]] = []
            with os.scandir(scope.directory(request.path)) as iterator:
                for index, entry in enumerate(iterator):
                    check()
                    if index >= 10000:
                        result["truncated"] = True
                        break
                    if index < request.offset:
                        continue
                    if len(entries) >= request.limit:
                        result.update(truncated=True, next_offset=index)
                        break
                    item: dict[str, Any] = {"name": entry.name, "kind": "blocked"}
                    try:
                        info = entry.stat(follow_symlinks=False)
                        attributes = getattr(info, "st_file_attributes", 0)
                        if not attributes & (0x400 | 0x1000 | 0x40000 | 0x400000):
                            if entry.is_dir(follow_symlinks=False):
                                item["kind"] = "directory"
                            elif entry.is_file(follow_symlinks=False) and info.st_nlink == 1:
                                item.update(kind="file", size_bytes=info.st_size)
                    except OSError:
                        pass  # Name visible, but inaccessible/reparse data is never followed.
                    entries.append(item)
            result["entries"] = entries
            return result
        raw = scope.read_bytes(request.path, check)
        text, encoding, _ = _detect_text(raw)
        lines = text.splitlines()
        selected: list[str] = []
        size = 0
        index = request.start_line - 1
        while index < len(lines) and len(selected) < request.max_lines:
            check()
            numbered = f"{index + 1}: {lines[index]}\n"
            line_bytes = len(numbered.encode("utf-8"))
            if size + line_bytes > request.max_bytes:
                if not selected:
                    raise AgentError(
                        "result_limit", "This line exceeds max_bytes; request a larger cap"
                    )
                break
            selected.append(numbered)
            size += line_bytes
            index += 1
        result.update(
            numbered_text="".join(selected),
            encoding=encoding,
            newline=_newline_style(text),
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            truncated=index < len(lines),
            next_line=index + 1 if index < len(lines) else None,
        )
        return result
