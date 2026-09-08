"""One-shot consent and at-most-once dispatch for opening a browser URL."""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from codito_protocol.desktop_action import BrowserChoice, DeviceDesktopInput, DeviceDesktopResult

from .approvals import ApprovalManager, ApprovalRisk, action_digest
from .errors import AgentError
from .read_tools import ToolResponse


@dataclass(slots=True)
class _DesktopAction:
    payload: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[dict[str, Any]]
    ensure_current: Callable[[], None]
    claimed: bool = False


class DesktopActionQueue:
    """Only the authenticated interactive tray can submit an action to Windows."""

    def __init__(self) -> None:
        self._pending: dict[str, _DesktopAction] = {}
        self._lock = threading.Lock()

    async def open_browser(
        self,
        url: str,
        deadline: datetime,
        *,
        ensure_current: Callable[[], None],
        browser: BrowserChoice = "default",
    ) -> dict[str, Any]:
        ensure_current()
        identifier = "desktop_" + secrets.token_urlsafe(24)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        pending = _DesktopAction(
            {
                "desktop_action_id": identifier,
                "action": "open_browser",
                "browser": browser,
                "url": url,
                "deadline_at": deadline.isoformat(),
            },
            loop,
            future,
            ensure_current,
        )
        with self._lock:
            self._pending[identifier] = pending
        try:
            return await asyncio.wait_for(
                future, max(0, (deadline - datetime.now(UTC)).total_seconds())
            )
        except TimeoutError as exc:
            if pending.claimed:
                raise AgentError(
                    "outcome_unknown",
                    "Desktop accepted the request but did not report whether the browser opened; "
                    "it will not be replayed automatically",
                ) from exc
            raise AgentError(
                "desktop_unavailable",
                "Windows desktop did not accept the request before its deadline",
            ) from exc
        finally:
            with self._lock:
                self._pending.pop(identifier, None)

    def next_request(self) -> dict[str, Any] | None:
        """Claim once; a lost tray response must never create a second browser tab."""
        with self._lock:
            for value in self._pending.values():
                if value.claimed or value.future.done():
                    continue
                value.claimed = True
                try:
                    value.ensure_current()
                except AgentError as exc:
                    value.loop.call_soon_threadsafe(self._reject, value.future, exc)
                    continue
                return dict(value.payload)
        return None

    def respond(self, identifier: str, result: dict[str, Any]) -> bool:
        with self._lock:
            value = self._pending.get(identifier)
            if value is None or not value.claimed or value.future.done():
                return False
            self._pending.pop(identifier)
        value.loop.call_soon_threadsafe(self._resolve, value, result)
        return True

    @staticmethod
    def _reject(future: asyncio.Future[dict[str, Any]], error: AgentError) -> None:
        if not future.done():
            future.set_exception(error)

    @staticmethod
    def _resolve(value: _DesktopAction, result: dict[str, Any]) -> None:
        if value.future.done():
            return
        if result.get("ok") is not True:
            value.future.set_exception(
                AgentError("desktop_unavailable", "Windows could not submit the browser request")
            )
        else:
            # The UI is only reporting OS acceptance, never page load or user-visible success.
            value.future.set_result({"ok": True})


class DeviceDesktopService:
    def __init__(
        self,
        approvals: ApprovalManager,
        actions: DesktopActionQueue,
        *,
        account_id: str,
        device_id: str,
    ) -> None:
        self.approvals = approvals
        self.actions = actions
        self.account_id = account_id
        self.device_id = device_id

    async def execute(
        self,
        request: DeviceDesktopInput,
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
            project_id="desktop_browser",
            project_title=f"Windows browser: {request.browser}",
            capability="desktop:open_url",
            action_digest=action_digest(request),
            connection_epoch=connection_epoch,
            deadline_at=deadline_at,
            risk=ApprovalRisk.NATIVE_EXECUTION,
            requested_network=True,
            summary=request.purpose
            + f"\nOPEN BROWSER ({request.browser}): "
            + request.url
            + "\nThe selected browser will contact this destination using your browser profile, "
            "including its existing website sessions. This is one-time approval; file, shell, "
            "and screenshot permissions do not authorize browser actions.",
            command={"action": "open_browser", "browser": request.browser, "url": request.url},
        )
        await self.approvals.request(approval, session_eligible=False)

        def ensure_current() -> None:
            self.approvals.ensure_current(generation, deadline_at)

        ensure_current()
        await self.actions.open_browser(
            request.url, deadline_at, ensure_current=ensure_current, browser=request.browser
        )
        ensure_current()
        result = DeviceDesktopResult(url=request.url, browser=request.browser)
        return ToolResponse(
            result.model_dump(mode="json"),
            f"Windows accepted the request to open the URL in the {request.browser} browser. "
            "This does not confirm that the page loaded.",
        )
