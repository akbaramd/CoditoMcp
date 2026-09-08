"""Approval precedes capture; only the interactive tray owns QScreen access."""

from __future__ import annotations

import asyncio
import secrets
import threading
from datetime import UTC, datetime
from typing import Any

from codito_protocol.screenshot import DeviceScreenshotInput, DeviceScreenshotResult

from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .errors import AgentError
from .read_tools import ToolResponse


class ScreenCaptureQueue:
    def __init__(self) -> None:
        self._pending: dict[
            str, tuple[dict[str, Any], asyncio.AbstractEventLoop, asyncio.Future[dict[str, Any]]]
        ] = {}
        self._lock = threading.Lock()

    async def capture(self, max_dimension: int, deadline: datetime) -> dict[str, Any]:
        identifier = "capture_" + secrets.token_urlsafe(24)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        with self._lock:
            self._pending[identifier] = (
                {
                    "capture_id": identifier,
                    "max_dimension": max_dimension,
                    "deadline_at": deadline.isoformat(),
                },
                loop,
                future,
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
            return next(
                (dict(value[0]) for value in self._pending.values() if not value[2].done()), None
            )

    def respond(self, identifier: str, result: dict[str, Any]) -> bool:
        with self._lock:
            value = self._pending.get(identifier)
        if value is None or value[2].done():
            return False

        def resolve() -> None:
            if value[2].done():
                return
            if result.get("ok") is False:
                value[2].set_exception(
                    AgentError(
                        "screenshot_unavailable",
                        "Desktop is locked, unavailable, or image exceeds limits",
                    )
                )
            else:
                value[2].set_result(result)

        value[1].call_soon_threadsafe(resolve)
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
        approval = ApprovalManager.build_request(
            account_id=self.account_id,
            grant_id=grant_id,
            link_id=link_id,
            device_id=self.device_id,
            project_id="screen_primary",
            project_title="Windows primary display",
            capability="screen:read",
            action_digest=action_digest(request),
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
            risk=ApprovalRisk.READ,
            summary=request.purpose
            + "\nSCREENSHOT: visible windows and private data on your primary display will be "
            "sent to ChatGPT and the relay. This is separate from file and shell access.",
        )
        await self.approvals.request(approval, session_eligible=False)
        self.approvals.ensure_current(generation, deadline_at)
        result = await self.capture.capture(request.max_dimension, deadline_at)
        self.approvals.ensure_current(generation, deadline_at)
        image = DeviceScreenshotResult.model_validate(result)
        return ToolResponse(
            image.model_dump(mode="json"),
            f"Captured Windows primary display: {image.width}x{image.height}, PNG. Image attached.",
        )
