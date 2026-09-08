"""Approval precedes capture; only the interactive tray owns QScreen access."""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from codito_protocol.screenshot import (
    DeviceDisplaysResult,
    DeviceScreenshotInput,
    DeviceScreenshotResult,
    DisplayCatalog,
)

from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .errors import AgentError
from .read_tools import ToolResponse


@dataclass(slots=True)
class _DesktopRequest:
    data: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[dict[str, Any]]
    deadline: datetime
    ensure_current: Callable[[], None] | None = None
    claimed: bool = False


class ScreenCaptureQueue:
    def __init__(self) -> None:
        self._pending: dict[str, _DesktopRequest] = {}
        self._lock = threading.Lock()

    async def list_displays(
        self, deadline: datetime, *, ensure_current: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        return await self._request({"action": "list_displays"}, deadline, ensure_current)

    async def capture(
        self,
        max_dimension: int,
        deadline: datetime,
        *,
        display_id: str,
        topology_id: str,
        ensure_current: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            {
                "action": "capture",
                "max_dimension": max_dimension,
                "display_id": display_id,
                "topology_id": topology_id,
            },
            deadline,
            ensure_current,
        )

    async def _request(
        self, data: dict[str, Any], deadline: datetime, ensure_current: Callable[[], None] | None
    ) -> dict[str, Any]:
        identifier = "capture_" + secrets.token_urlsafe(24)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        with self._lock:
            self._pending[identifier] = _DesktopRequest(
                {
                    **data,
                    "capture_id": identifier,
                    "deadline_at": deadline.isoformat(),
                },
                loop,
                future,
                deadline,
                ensure_current,
            )
        try:
            return await asyncio.wait_for(
                future, max(0, (deadline - datetime.now(UTC)).total_seconds())
            )
        except TimeoutError as exc:
            raise AgentError(
                "screenshot_unavailable", "Windows desktop did not capture before the deadline"
            ) from exc
        finally:
            with self._lock:
                self._pending.pop(identifier, None)

    def next_request(self) -> dict[str, Any] | None:
        with self._lock:
            for value in self._pending.values():
                if (
                    not value.claimed
                    and not value.future.done()
                    and value.deadline > datetime.now(UTC)
                ):
                    # Claim once: an interrupted IPC response cannot trigger duplicate pixels.
                    value.claimed = True
                    if value.ensure_current is not None:
                        try:
                            value.ensure_current()
                        except AgentError as exc:
                            value.loop.call_soon_threadsafe(self._reject, value.future, exc)
                            continue
                    return dict(value.data)
        return None

    @staticmethod
    def _reject(future: asyncio.Future[dict[str, Any]], error: AgentError) -> None:
        if not future.done():
            future.set_exception(error)

    def respond(self, identifier: str, result: dict[str, Any]) -> bool:
        with self._lock:
            value = self._pending.get(identifier)
            if (
                value is None
                or not value.claimed
                or value.future.done()
                or value.deadline <= datetime.now(UTC)
            ):
                return False
            self._pending.pop(identifier)

        def resolve() -> None:
            if value.future.done():
                return
            if result.get("ok") is False:
                value.future.set_exception(
                    AgentError(
                        "screenshot_unavailable",
                        "Desktop is locked, unavailable, or image exceeds limits",
                    )
                )
            else:
                value.future.set_result(result)

        value.loop.call_soon_threadsafe(resolve)
        return True


class DeviceScreenshotService:
    def __init__(
        self,
        approvals: ApprovalManager,
        capture: ScreenCaptureQueue,
        *,
        account_id: str,
        device_id: str,
    ) -> None:
        self.approvals = approvals
        self.capture = capture
        self.account_id = account_id
        self.device_id = device_id

    async def execute(
        self,
        request: DeviceScreenshotInput,
        *,
        grant_id: str,
        link_id: str,
        connection_epoch: int,
        deadline_at: datetime,
    ) -> ToolResponse:
        generation = self.approvals.generation

        def ensure_current() -> None:
            self.approvals.ensure_current(generation, deadline_at)

        catalog = DisplayCatalog.model_validate(
            await self.capture.list_displays(deadline_at, ensure_current=ensure_current)
        )
        self.approvals.ensure_current(generation, deadline_at)
        if request.action == "list_displays":
            result = DeviceDisplaysResult(**catalog.model_dump()).model_dump(mode="json")
            return ToolResponse(
                result, f"{len(catalog.displays)} Windows displays; no pixels captured."
            )
        display = next(
            (
                item
                for item in catalog.displays
                if (item.primary if request.display == "primary" else item.id == request.display)
            ),
            None,
        )
        if display is None:
            raise AgentError(
                "screenshot_unavailable", "Selected display is unavailable; list displays again"
            )
        identity = action_digest([display.identity, catalog.topology_id])
        persistent = (
            display.persistent_permission_supported and self.approvals.supports_saved_permissions
        )
        approval = ApprovalManager.build_request(
            account_id=self.account_id,
            grant_id=grant_id,
            link_id=link_id,
            device_id=self.device_id,
            project_id=display.id,
            project_title=display.label,
            capability="screen:read",
            action_digest=action_digest([request.model_dump(mode="json"), display.id, identity]),
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
            risk=ApprovalRisk.READ,
            summary=request.purpose
            + f"\nSCREENSHOT: {display.label} ({display.width}x{display.height}). Visible windows "
            "and private data on this display will be sent to ChatGPT and the relay. "
            "Always allow permits future captures of this selected display configuration "
            "for this account "
            "and connection grant until revoked, signed out, or the monitor layout changes. "
            "This is separate from file and shell access.",
        )
        await self.approvals.request(
            approval,
            session_eligible=False,
            screen_scope=(display.id, identity, display.label) if persistent else None,
        )
        self.approvals.ensure_current(generation, deadline_at)
        pixels = await self.capture.capture(
            request.max_dimension,
            deadline_at,
            display_id=display.id,
            topology_id=catalog.topology_id,
            ensure_current=ensure_current,
        )
        self.approvals.ensure_current(generation, deadline_at)
        image = DeviceScreenshotResult.model_validate(pixels)
        if image.display != display.id:
            raise AgentError("screenshot_unavailable", "Desktop returned a different display")
        return ToolResponse(
            image.model_dump(mode="json"),
            f"Captured {display.label}: {image.width}x{image.height}, PNG. Image attached.",
        )
