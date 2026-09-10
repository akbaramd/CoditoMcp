from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.parse import urlunsplit as _urlunsplit

from .errors import AgentError

MAX_SCREENSHOT_BYTES = 600_000
MAX_ELEMENTS = 500
MAX_EVENT_TEXT = 1_024
MAX_EVENTS = 100
MAX_MATCHED_RULES = 40
BROWSER_CLOSE_TIMEOUT_SECONDS = 30.0
BROWSER_LAUNCH_RECOVERY_TIMEOUT_SECONDS = 70.0

SELECTED_STYLES = (
    "display",
    "position",
    "width",
    "height",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "gap",
    "row-gap",
    "column-gap",
    "font-family",
    "font-size",
    "font-weight",
    "line-height",
    "color",
    "background-color",
    "border-top-width",
    "border-right-width",
    "border-bottom-width",
    "border-left-width",
    "border-top-color",
    "border-right-color",
    "border-bottom-color",
    "border-left-color",
    "border-radius",
    "outline-style",
    "outline-width",
    "outline-color",
    "outline-offset",
    "box-shadow",
    "cursor",
    "pointer-events",
    "align-items",
    "justify-content",
    "flex-direction",
    "overflow",
    "opacity",
    "visibility",
)

SAFE_ATTRIBUTES = frozenset(
    {"id", "class", "data-codito-source", "data-codito-host-source", "aria-label"}
)
SENSITIVE_AUTOCOMPLETE = frozenset(
    {
        "current-password",
        "new-password",
        "one-time-code",
        "webauthn",
        "cc-name",
        "cc-given-name",
        "cc-additional-name",
        "cc-family-name",
        "cc-number",
        "cc-csc",
        "cc-exp",
        "cc-exp-month",
        "cc-exp-year",
        "cc-type",
    }
)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)(["']?(?:authorization|access[_-]?token|refresh[_-]?token|token|"""
    r"""client[_-]?secret|secret|password|passwd|api[_-]?key)["']?\s*[:=]\s*)"""
    r"""(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)"""
)
_SENSITIVE_HEADER = re.compile(
    r"""(?ix)(["']?(?:authorization|proxy-authorization|cookie|set-cookie)["']?"""
    r"""\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\r\n]*)"""
)
_AUTH_SCHEME = re.compile(r"(?i)\b(?:bearer|basic|digest)\s+[^\r\n]+")
_URL = re.compile(r"https?://[^\s\]\[<>\"']+")
_FILE_URL = re.compile(r"(?i)\bfile:/+(?:[^\s\]\[<>\"']+)")
_WINDOWS_PATH_MARKER = re.compile(r"""(?ix)(?<![a-z0-9_])(?:[a-z]:[\\/]|\\\\[^\\/\r\n]+[\\/])""")


def _frozen_browsers_path() -> str:
    package_root = Path(sys.executable).resolve().parent.parent
    return str(package_root / "browsers")


def _origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    default = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    return _urlunsplit((parsed.scheme, host if default else f"{host}:{port}", "", "", ""))


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return "/unsupported-resource"
        if parsed.scheme in {"http", "https"} and not parsed.hostname:
            return "/invalid-resource"
        if not parsed.scheme:
            path = parsed.path or "/unknown-resource"
            return path if path.startswith("/") else f"/{path}"
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return _urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "/invalid-resource"


def _safe_text(value: object) -> str:
    text = str(value).replace("\x00", "")[:MAX_EVENT_TEXT]
    # A quoted or unquoted Windows path can legally contain spaces, so trying
    # to redact only the matching token risks leaking its suffix. Drop the
    # whole bounded diagnostic whenever an absolute/UNC/file path is present.
    if _FILE_URL.search(text) or _WINDOWS_PATH_MARKER.search(text):
        return "[local path redacted]"
    text = _URL.sub(lambda match: _safe_url(match.group(0)), text)
    text = _SENSITIVE_HEADER.sub(lambda match: f'{match.group(1)}"[redacted]"', text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f'{match.group(1)}"[redacted]"', text)
    return _AUTH_SCHEME.sub("[authorization redacted]", text)


def _is_sensitive_autocomplete(value: str) -> bool:
    return not SENSITIVE_AUTOCOMPLETE.isdisjoint(value.lower().split())


def _string(strings: list[Any], index: Any) -> str:
    if isinstance(index, int) and 0 <= index < len(strings):
        value = strings[index]
        return str(value) if value is not None else ""
    return ""


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _quad_box(quad: object) -> dict[str, float] | None:
    if not isinstance(quad, list) or len(quad) < 8:
        return None
    try:
        xs = [float(quad[index]) for index in range(0, 8, 2)]
        ys = [float(quad[index]) for index in range(1, 8, 2)]
    except (TypeError, ValueError):
        return None
    return {
        "x": round(min(xs), 2),
        "y": round(min(ys), 2),
        "width": round(max(xs) - min(xs), 2),
        "height": round(max(ys) - min(ys), 2),
    }


@dataclass(frozen=True, slots=True)
class BrowserSnapshot:
    png: bytes
    width: int
    height: int
    elements: list[dict[str, Any]]
    console: list[dict[str, Any]]
    network: list[dict[str, Any]]
    truncated: bool
    document_token: str
    url: str
    title: str
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ManagedPage:
    context: Any
    page: Any
    cdp: Any
    allowed_origin: str
    viewport: tuple[int, int]
    console: deque[dict[str, Any]]
    network: deque[dict[str, Any]]
    last_safe_url: str
    document_counter: int = 0
    mutation_counter: int = 0
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    layout_fingerprint: str = ""
    blocked_navigation: bool = False
    restore_task: asyncio.Task[None] | None = None
    blocked_navigation_event: asyncio.Event = field(default_factory=asyncio.Event)
    navigation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class PlaywrightAdapter:
    """Narrow, lazy Playwright/CDP adapter with no general browser-eval API."""

    def __init__(
        self,
        *,
        visible: bool = True,
        executable_path: Path | None = None,
        fallback_channel: str | None = "msedge",
    ) -> None:
        self.visible = visible
        if executable_path is not None and (
            not executable_path.is_absolute() or not executable_path.is_file()
        ):
            raise AgentError("invalid_config", "Managed browser executable is unavailable")
        if fallback_channel not in {None, "msedge"}:
            raise AgentError("invalid_config", "Unsupported managed browser fallback channel")
        self.executable_path = executable_path
        self.fallback_channel = fallback_channel
        self._playwright: Any | None = None
        self._loader_guard = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._orphan_contexts: list[Any] = []

    def _track_cleanup(self, coroutine: Any, *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        return task

    def _quarantine_context(self, context: Any) -> None:
        if not any(candidate is context for candidate in self._orphan_contexts):
            self._orphan_contexts.append(context)

    def _track_context_cleanup(self, context: Any, *, name: str) -> asyncio.Task[Any]:
        task = self._track_cleanup(self._close_unreturned_context(context), name=name)

        def quarantine_on_failure(done: asyncio.Task[Any]) -> None:
            if done.cancelled() or done.exception() is not None:
                self._quarantine_context(context)

        task.add_done_callback(quarantine_on_failure)
        return task

    @staticmethod
    async def _await_shielded_cleanup(task: asyncio.Task[Any]) -> BaseException | None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A second cancellation does not revoke cleanup ownership. The
            # adapter retains the task until its bounded cleanup completes.
            return None
        except BaseException as exc:
            return exc
        return None

    async def _load(self) -> Any:
        async with self._loader_guard:
            if self._playwright is not None:
                return self._playwright
            if getattr(sys, "frozen", False):
                # A release must never let an inherited environment redirect the
                # managed runtime to an unreviewed browser installation.
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _frozen_browsers_path()
                os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise AgentError(
                    "browser_unavailable", "Managed Chromium support is not installed"
                ) from exc
            manager = async_playwright()
            try:
                self._playwright = await manager.start()
            except asyncio.CancelledError:
                cleanup = self._track_cleanup(
                    asyncio.wait_for(manager.__aexit__(None, None, None), 30.0),
                    name="codito-playwright-driver-start-cleanup",
                )
                cleanup_error = await self._await_shielded_cleanup(cleanup)
                if cleanup_error is not None:
                    raise AgentError(
                        "browser_close_failed",
                        "A cancelled Playwright driver start could not be closed",
                        retryable=True,
                    ) from cleanup_error
                raise
            except Exception as exc:
                raise AgentError("browser_unavailable", "Playwright could not initialize") from exc
            return self._playwright

    async def _launch_persistent_context(
        self,
        runtime: Any,
        profile_directory: Path,
        launch_options: dict[str, Any],
        *,
        frozen: bool,
    ) -> Any:
        try:
            return await runtime.chromium.launch_persistent_context(
                str(profile_directory), **launch_options
            )
        except Exception as first_error:
            if frozen or self.executable_path is not None or self.fallback_channel is None:
                raise AgentError(
                    "browser_unavailable",
                    "Managed Chromium could not start; no download was attempted",
                ) from first_error
            try:
                return await runtime.chromium.launch_persistent_context(
                    str(profile_directory), **launch_options, channel=self.fallback_channel
                )
            except Exception as exc:
                raise AgentError(
                    "browser_unavailable",
                    "Managed Chromium or the installed Edge fallback could not start; "
                    "no download was attempted",
                ) from exc

    async def _close_unreturned_context(self, context: Any) -> None:
        await asyncio.wait_for(context.close(), BROWSER_CLOSE_TIMEOUT_SECONDS)

    async def _recover_cancelled_launch(self, launch_task: asyncio.Task[Any]) -> None:
        try:
            context = await asyncio.wait_for(
                asyncio.shield(launch_task), BROWSER_LAUNCH_RECOVERY_TIMEOUT_SECONDS
            )
        except TimeoutError:
            launch_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(launch_task), BROWSER_CLOSE_TIMEOUT_SECONDS)
            return
        except BaseException:
            return
        try:
            await self._close_unreturned_context(context)
        except BaseException:
            self._quarantine_context(context)
            raise

    async def open(
        self,
        *,
        profile_directory: Path,
        url: str,
        allowed_origin: str,
        viewport: tuple[int, int],
    ) -> ManagedPage:
        runtime = await self._load()
        await asyncio.to_thread(profile_directory.mkdir, parents=True, exist_ok=True)
        launch_options: dict[str, Any] = {
            "headless": not self.visible,
            "viewport": {"width": viewport[0], "height": viewport[1]},
            "device_scale_factor": 1,
            "accept_downloads": False,
            "chromium_sandbox": True,
            "timeout": 30_000,
        }
        frozen = bool(getattr(sys, "frozen", False))
        if self.executable_path is not None and not frozen:
            launch_options["executable_path"] = str(self.executable_path)
        launch_task = asyncio.create_task(
            self._launch_persistent_context(
                runtime,
                profile_directory,
                launch_options,
                frozen=frozen,
            ),
            name="codito-playwright-context-launch",
        )
        self._cleanup_tasks.add(launch_task)
        launch_task.add_done_callback(self._cleanup_tasks.discard)
        try:
            context = await asyncio.shield(launch_task)
        except asyncio.CancelledError:
            cleanup = self._track_cleanup(
                self._recover_cancelled_launch(launch_task),
                name="codito-playwright-context-launch-cleanup",
            )
            cleanup_error = await self._await_shielded_cleanup(cleanup)
            if cleanup_error is not None:
                raise AgentError(
                    "browser_close_failed",
                    "A cancelled managed browser launch could not be closed",
                    retryable=True,
                ) from cleanup_error
            raise

        record = ManagedPage(
            context=context,
            page=None,
            cdp=None,
            allowed_origin=allowed_origin,
            viewport=viewport,
            console=deque(maxlen=MAX_EVENTS),
            network=deque(maxlen=MAX_EVENTS),
            last_safe_url=url,
        )
        try:
            restored_pages = list(context.pages)
            await context.route("**/*", lambda route, request: self._route(record, route, request))
            page = await context.new_page()
            record.page = page
            for restored_page in restored_pages:
                await restored_page.close()
            context.on("page", lambda popup: asyncio.create_task(self._close_popup(record, popup)))
            page.on("download", lambda download: asyncio.create_task(download.cancel()))
            page.on(
                "filechooser",
                lambda chooser: asyncio.create_task(self._clear_file_chooser(chooser)),
            )
            page.on("console", lambda message: self._console(record, message))
            page.on("pageerror", lambda error: self._page_error(record, error))
            page.on("response", lambda response: self._response(record, response))
            page.on("requestfailed", lambda request: self._request_failed(record, request))
            page.on("framenavigated", lambda frame: self._navigated(record, frame))
            record.cdp = await context.new_cdp_session(page)
            await record.cdp.send("DOM.enable")
            await record.cdp.send("CSS.enable")
            await record.cdp.send("Accessibility.enable")
            for event_name in (
                "DOM.documentUpdated",
                "DOM.attributeModified",
                "DOM.attributeRemoved",
                "DOM.characterDataModified",
                "DOM.childNodeCountUpdated",
                "DOM.childNodeInserted",
                "DOM.childNodeRemoved",
                "DOM.shadowRootPushed",
                "DOM.shadowRootPopped",
                "CSS.styleSheetAdded",
                "CSS.styleSheetRemoved",
                "CSS.styleSheetChanged",
            ):
                record.cdp.on(event_name, lambda _event: self._mutated(record))
            # CDP emits mutation events only for materialized document nodes.
            await record.cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if _origin(page.url) != allowed_origin:
                raise AgentError("navigation_blocked", "The page left its approved loopback origin")
            # Navigation invalidates the tree materialized above. Materialize the
            # approved document so subsequent HMR mutations emit DOM events.
            await record.cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
            return record
        except BaseException as exc:
            cleanup = self._track_context_cleanup(
                context,
                name="codito-playwright-open-cleanup",
            )
            cleanup_error = await self._await_shielded_cleanup(cleanup)
            if cleanup_error is not None:
                self._quarantine_context(context)
                raise AgentError(
                    "browser_close_failed",
                    "An incomplete managed browser open could not be closed",
                    retryable=True,
                ) from cleanup_error
            if isinstance(exc, AgentError):
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AgentError(
                "browser_navigation_failed", "Managed Chromium could not open the route"
            ) from exc

    async def _route(self, record: ManagedPage, route: Any, request: Any) -> None:
        try:
            top_navigation = request.is_navigation_request() and request.frame.parent_frame is None
            if top_navigation and _origin(request.url) != record.allowed_origin:
                await route.abort("blockedbyclient")
                self._mark_blocked_navigation(record)
                return
            await route.continue_()
        except Exception:
            with contextlib.suppress(Exception):
                await route.abort("blockedbyclient")

    async def _close_popup(self, record: ManagedPage, popup: Any) -> None:
        if popup is record.page:
            return
        with contextlib.suppress(Exception):
            await popup.close()

    async def _clear_file_chooser(self, chooser: Any) -> None:
        # Merely registering a Playwright filechooser listener prevents the
        # operating-system picker. Clear the intercepted chooser as a second
        # line of defence; callers have no API for supplying file paths.
        with contextlib.suppress(Exception):
            await chooser.set_files([])

    def _mark_blocked_navigation(self, record: ManagedPage) -> None:
        if record.closed:
            return
        record.blocked_navigation = True
        record.blocked_navigation_event.set()
        if record.restore_task is None or record.restore_task.done():
            record.restore_task = asyncio.create_task(
                self._restore_safe_page(record), name="codito-browser-navigation-restore"
            )

    async def _restore_safe_page(self, record: ManagedPage) -> None:
        async with record.navigation_lock:
            if record.closed or not record.blocked_navigation:
                return
            try:
                # Reload even when an aborted HTTP request has not committed
                # yet. Chromium may otherwise commit chrome-error:// after the
                # route callback returns, racing a conditional URL check.
                await record.page.goto(
                    record.last_safe_url,
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )
            except Exception:
                # The following operation reports a generic navigation failure;
                # URLs, response bodies and browser state never cross this boundary.
                return
            finally:
                record.blocked_navigation = False

    def _console(self, record: ManagedPage, message: Any) -> None:
        record.console.append({"level": str(message.type)[:32], "text": _safe_text(message.text)})

    def _page_error(self, record: ManagedPage, error: Any) -> None:
        record.console.append({"level": "error", "text": _safe_text(error)})

    def _response(self, record: ManagedPage, response: Any) -> None:
        if not bool(response.ok):
            record.network.append(
                {
                    "method": str(response.request.method)[:16],
                    "url": _safe_url(response.url),
                    "status": int(response.status),
                    "ok": False,
                }
            )

    def _request_failed(self, record: ManagedPage, request: Any) -> None:
        record.network.append(
            {
                "method": str(request.method)[:16],
                "url": _safe_url(request.url),
                "status": None,
                "ok": False,
                "error": _safe_text(request.failure or "request failed")[:256],
            }
        )

    def _navigated(self, record: ManagedPage, frame: Any) -> None:
        if record.page is not None and frame == record.page.main_frame:
            record.document_counter += 1
            if _origin(frame.url) == record.allowed_origin:
                record.last_safe_url = frame.url
            else:
                # Playwright routing does not observe every scheme (notably
                # about:blank). Main-frame navigation is therefore also
                # enforced at the committed-navigation boundary.
                self._mark_blocked_navigation(record)

    @staticmethod
    def _mutated(record: ManagedPage) -> None:
        record.mutation_counter += 1

    async def snapshot(self, record: ManagedPage, *, max_elements: int) -> BrowserSnapshot:
        self._ensure_open(record)
        limit = min(max(max_elements, 1), MAX_ELEMENTS)
        try:
            for attempt in range(2):
                # A navigation invalidates CDP's materialized tree. Refresh it
                # before minting a generation so HMR mutations remain observable.
                await record.cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
                await self._refresh_scroll_state(record)
                await self._refresh_layout_fingerprint(record)
                before = self.document_token(record)
                png = await record.page.screenshot(type="png", full_page=False)
                if len(png) > MAX_SCREENSHOT_BYTES:
                    raise AgentError(
                        "capture_too_large", "The viewport PNG exceeds the safe response limit"
                    )
                raw = await record.cdp.send(
                    "DOMSnapshot.captureSnapshot",
                    {"computedStyles": list(SELECTED_STYLES), "includePaintOrder": True},
                )
                captured_fingerprint = self._layout_fingerprint(raw)
                ax = await record.cdp.send("Accessibility.getFullAXTree")
                title = _safe_text(await record.page.title())[:1_024]
                await self._refresh_scroll_state(record)
                await self._refresh_layout_fingerprint(record)
                after = self.document_token(record)
                if before == after and (
                    captured_fingerprint is None
                    or captured_fingerprint == record.layout_fingerprint
                ):
                    break
                if attempt == 1:
                    raise AgentError(
                        "stale_snapshot",
                        "The page kept changing during capture; wait and try again",
                    )
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError(
                "browser_capture_failed", "The frontend snapshot could not be captured"
            ) from exc
        elements, truncated = self._semantic_elements(raw, ax, limit, record.viewport)
        documents = raw.get("documents") if isinstance(raw.get("documents"), list) else []
        warnings = (
            ["Subframe documents are excluded from this snapshot."] if len(documents) > 1 else []
        )
        self._ensure_open(record)
        network = list(record.network)[-25:]
        # Network diagnostics are failure-only and drained after a capture so
        # a repaired request does not keep resurfacing in every later snapshot.
        record.network.clear()
        return BrowserSnapshot(
            png=bytes(png),
            width=record.viewport[0],
            height=record.viewport[1],
            elements=elements,
            console=list(record.console)[-25:],
            network=network,
            truncated=truncated,
            document_token=after,
            url=_safe_url(record.page.url),
            title=title,
            warnings=warnings,
        )

    def _semantic_elements(
        self,
        raw: dict[str, Any],
        ax_raw: dict[str, Any],
        limit: int,
        viewport: tuple[int, int],
    ) -> tuple[list[dict[str, Any]], bool]:
        strings = _as_list(raw.get("strings"))
        documents = _as_list(raw.get("documents"))
        if not documents or not isinstance(documents[0], dict):
            return [], False
        document = _as_dict(documents[0])
        nodes = _as_dict(document.get("nodes"))
        layout = _as_dict(document.get("layout"))
        layout_indices = _as_list(layout.get("nodeIndex"))
        bounds = _as_list(layout.get("bounds"))
        styles = _as_list(layout.get("styles"))
        backend_ids = _as_list(nodes.get("backendNodeId"))
        names = _as_list(nodes.get("nodeName"))
        values = _as_list(nodes.get("nodeValue"))
        node_types = _as_list(nodes.get("nodeType"))
        attributes = _as_list(nodes.get("attributes"))
        parents = _as_list(nodes.get("parentIndex"))
        try:
            scroll_x = float(document.get("scrollOffsetX", 0))
            scroll_y = float(document.get("scrollOffsetY", 0))
        except (TypeError, ValueError):
            scroll_x = 0.0
            scroll_y = 0.0

        ax_by_backend: dict[int, dict[str, Any]] = {}
        for item in ax_raw.get("nodes", []):
            if not isinstance(item, dict) or not isinstance(item.get("backendDOMNodeId"), int):
                continue
            role = _as_dict(item.get("role"))
            name = _as_dict(item.get("name"))
            ax_value: dict[str, Any] = {
                "role": _safe_text(role.get("value", ""))[:128],
                "name": _safe_text(name.get("value", ""))[:1_024],
            }
            for prop in item.get("properties", []):
                if not isinstance(prop, dict) or prop.get("name") not in {
                    "disabled",
                    "focused",
                    "expanded",
                    "checked",
                }:
                    continue
                value = _as_dict(prop.get("value"))
                candidate = value.get("value")
                if isinstance(candidate, bool) or candidate == "mixed":
                    ax_value[str(prop["name"])] = candidate
            ax_by_backend[item["backendDOMNodeId"]] = ax_value

        # DOMSnapshot bounds use document coordinates. Convert to viewport
        # coordinates before matching elements to the captured viewport PNG.
        layout_by_node: dict[int, tuple[dict[str, float], object]] = {}
        for layout_position, node_index in enumerate(layout_indices):
            if not isinstance(node_index, int):
                continue
            box_raw = bounds[layout_position] if layout_position < len(bounds) else None
            if not isinstance(box_raw, list) or len(box_raw) < 4:
                continue
            try:
                box = {
                    "x": round(float(box_raw[0]) - scroll_x, 2),
                    "y": round(float(box_raw[1]) - scroll_y, 2),
                    "width": round(float(box_raw[2]), 2),
                    "height": round(float(box_raw[3]), 2),
                }
            except (TypeError, ValueError):
                continue
            left = max(0.0, box["x"])
            top = max(0.0, box["y"])
            right = min(float(viewport[0]), box["x"] + box["width"])
            bottom = min(float(viewport[1]), box["y"] + box["height"])
            if right <= left or bottom <= top:
                continue
            box = {
                "x": round(left, 2),
                "y": round(top, 2),
                "width": round(right - left, 2),
                "height": round(bottom - top, 2),
            }
            style_values = styles[layout_position] if layout_position < len(styles) else []
            layout_by_node[node_index] = (box, style_values)

        candidate_order: list[int] = []
        candidates: dict[int, dict[str, Any]] = {}
        for node_index in layout_indices:
            if not isinstance(node_index, int) or node_index not in layout_by_node:
                continue
            if node_index >= len(backend_ids) or node_index >= len(names):
                continue
            tag = _string(strings, names[node_index]).lower()
            if (
                not tag
                # DOMSnapshot exposes generated CSS content as synthetic nodes
                # named ``::before``/``::after``. They are not addressable DOM
                # elements and cannot safely participate in the element registry.
                or tag.startswith(("#", "::"))
                or tag
                in {
                    "html",
                    "head",
                    "script",
                    "style",
                    "meta",
                    "link",
                    "template",
                    "noscript",
                }
                or (node_index < len(node_types) and node_types[node_index] != 1)
            ):
                continue
            box, style_values = layout_by_node[node_index]
            backend_id = backend_ids[node_index]
            if not isinstance(backend_id, int):
                continue
            attrs = self._decode_attributes(
                strings, attributes[node_index] if node_index < len(attributes) else []
            )
            style = {
                name: _string(strings, style_values[index])[:256]
                for index, name in enumerate(SELECTED_STYLES)
                if isinstance(style_values, list)
                and index < len(style_values)
                and _string(strings, style_values[index])
            }
            if style.get("visibility") in {"hidden", "collapse"} or style.get("opacity") == "0":
                continue
            text = _string(strings, values[node_index])[:1_024] if node_index < len(values) else ""
            item = {
                "backend_node_id": backend_id,
                "parent_backend_node_id": None,
                "tag": tag[:64],
                "text": _safe_text(text)[:1_024],
                "box": box,
                "attributes": attrs,
                "computed": style,
                "accessibility": ax_by_backend.get(backend_id, {"role": "", "name": ""}),
            }
            if node_index not in candidates:
                candidate_order.append(node_index)
                candidates[node_index] = item

        # Large pages often put hundreds of anonymous layout wrappers before
        # the first control. Admit actionable/AX-named/source-instrumented nodes
        # first, then their closest available ancestors, then generic content.
        actionable_tags = {
            "a",
            "button",
            "details",
            "input",
            "option",
            "select",
            "summary",
            "textarea",
        }
        priority_nodes = [
            node_index
            for node_index in candidate_order
            if (
                candidates[node_index]["tag"] in actionable_tags
                or str(candidates[node_index]["accessibility"].get("role", "")).lower()
                not in {"", "generic", "none", "presentation"}
                or bool(candidates[node_index]["accessibility"].get("name"))
                or "data-codito-source" in candidates[node_index]["attributes"]
                or "data-codito-host-source" in candidates[node_index]["attributes"]
            )
        ]
        selected: set[int] = set(priority_nodes[:limit])
        for node_index in priority_nodes:
            current = node_index
            seen_ancestors: set[int] = set()
            while len(selected) < limit and current < len(parents):
                parent = parents[current]
                if not isinstance(parent, int) or parent < 0 or parent in seen_ancestors:
                    break
                seen_ancestors.add(parent)
                if parent in candidates:
                    selected.add(parent)
                current = parent
        for node_index in candidate_order:
            if len(selected) >= limit:
                break
            selected.add(node_index)

        result = [
            candidates[node_index] for node_index in candidate_order if node_index in selected
        ]
        result_by_node = {
            node_index: candidates[node_index]
            for node_index in candidate_order
            if node_index in selected
        }
        truncated = len(candidates) > len(result)

        def nearest_included_ancestor(node_index: int) -> tuple[int, dict[str, Any]] | None:
            seen: set[int] = set()
            current = node_index
            while current < len(parents) and isinstance(parents[current], int):
                parent = parents[current]
                if parent < 0 or parent in seen:
                    return None
                included = result_by_node.get(parent)
                if included is not None:
                    return parent, included
                seen.add(parent)
                current = parent
            return None

        # Parent references may only point to another returned element.
        for node_index, item in result_by_node.items():
            ancestor = nearest_included_ancestor(node_index)
            if ancestor is not None:
                item["parent_backend_node_id"] = ancestor[1]["backend_node_id"]

        # Element nodeValue is normally empty. Assign visible text-node values to
        # their nearest returned element so generic content remains meaningful.
        text_by_node: dict[int, list[str]] = {}
        blocked_text_ancestors = {"head", "script", "style", "template", "noscript"}
        for node_index in layout_by_node:
            is_text = (node_index < len(node_types) and node_types[node_index] == 3) or (
                node_index < len(names) and _string(strings, names[node_index]).lower() == "#text"
            )
            if not is_text or node_index >= len(values):
                continue
            _box, text_style_values = layout_by_node[node_index]
            text_style = {
                name: _string(strings, text_style_values[index])
                for index, name in enumerate(SELECTED_STYLES)
                if isinstance(text_style_values, list) and index < len(text_style_values)
            }
            if (
                text_style.get("visibility") in {"hidden", "collapse"}
                or text_style.get("opacity") == "0"
            ):
                continue
            text = " ".join(_string(strings, values[node_index]).split())
            if not text:
                continue
            current = node_index
            seen_text_ancestors: set[int] = set()
            target: tuple[int, dict[str, Any]] | None = None
            while current < len(parents) and isinstance(parents[current], int):
                parent = parents[current]
                if parent < 0 or parent in seen_text_ancestors:
                    break
                seen_text_ancestors.add(parent)
                if (
                    parent < len(names)
                    and _string(strings, names[parent]).lower() in blocked_text_ancestors
                ):
                    target = None
                    break
                included = result_by_node.get(parent)
                if included is not None:
                    target = (parent, included)
                    break
                current = parent
            if target is not None:
                text_by_node.setdefault(target[0], []).append(text)
        for node_index, chunks in text_by_node.items():
            combined = " ".join(chunks)
            existing = str(result_by_node[node_index].get("text", ""))
            if existing:
                combined = f"{existing} {combined}"
            result_by_node[node_index]["text"] = _safe_text(combined)[:1_024]

        return result, truncated

    def _decode_attributes(self, strings: list[Any], encoded: object) -> dict[str, str]:
        if not isinstance(encoded, list):
            return {}
        result: dict[str, str] = {}
        for index in range(0, min(len(encoded) - 1, 64), 2):
            name = _string(strings, encoded[index]).lower()
            if name in SAFE_ATTRIBUTES:
                result[name] = _safe_text(_string(strings, encoded[index + 1]))[:512]
        return result

    async def inspect(
        self,
        record: ManagedPage,
        *,
        backend_node_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_open(record)
        await self._refresh_scroll_state(record)
        await self._refresh_layout_fingerprint(record)
        try:
            if backend_node_id is None:
                if x is None or y is None:
                    raise AgentError("invalid_request", "Inspect needs an element or coordinates")
                if x < 0 or y < 0 or x >= record.viewport[0] or y >= record.viewport[1]:
                    raise AgentError(
                        "invalid_request", "Inspect coordinates are outside the viewport"
                    )
                located = await record.cdp.send(
                    "DOM.getNodeForLocation", {"x": x, "y": y, "includeUserAgentShadowDOM": True}
                )
                backend_node_id = located.get("backendNodeId")
            if not isinstance(backend_node_id, int):
                raise AgentError("element_not_found", "No inspectable element was found")
            node_id = await self._frontend_node_id(record, backend_node_id)
            described = await record.cdp.send("DOM.describeNode", {"nodeId": node_id})
            node = described.get("node") if isinstance(described.get("node"), dict) else {}
            # Pixel hit testing may resolve a text or pseudo node. Normalize it
            # to the nearest real element before exposing the public tag.
            seen_nodes: set[int] = set()
            while node.get("nodeType") != 1:
                parent_id = node.get("parentId")
                if not isinstance(parent_id, int) or parent_id <= 0 or parent_id in seen_nodes:
                    raise AgentError("element_not_found", "No inspectable element was found")
                seen_nodes.add(parent_id)
                node_id = parent_id
                described = await record.cdp.send("DOM.describeNode", {"nodeId": node_id})
                node = described.get("node") if isinstance(described.get("node"), dict) else {}
            normalized_backend = node.get("backendNodeId")
            if not isinstance(normalized_backend, int):
                raise AgentError("element_not_found", "No inspectable element was found")
            backend_node_id = normalized_backend
            await self._ensure_main_document_element(record, backend_node_id)
            box_model = await record.cdp.send("DOM.getBoxModel", {"backendNodeId": backend_node_id})
            computed_raw = await record.cdp.send("CSS.getComputedStyleForNode", {"nodeId": node_id})
            matched_raw = await record.cdp.send("CSS.getMatchedStylesForNode", {"nodeId": node_id})
            ax_raw = await record.cdp.send(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend_node_id, "fetchRelatives": False},
            )
            await self._refresh_scroll_state(record)
            await self._refresh_layout_fingerprint(record)
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError("element_not_found", "The element no longer exists") from exc
        tag = str(node.get("nodeName", "")).lower()[:64]
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", tag):
            raise AgentError("element_not_found", "No inspectable element was found")
        attrs_list = _as_list(node.get("attributes"))
        attrs = {
            str(attrs_list[index]).lower(): _safe_text(attrs_list[index + 1])[:512]
            for index in range(0, min(len(attrs_list) - 1, 64), 2)
            if str(attrs_list[index]).lower() in SAFE_ATTRIBUTES
        }
        computed = {
            item["name"]: _safe_text(item.get("value", ""))[:256]
            for item in computed_raw.get("computedStyle", [])
            if isinstance(item, dict) and item.get("name") in SELECTED_STYLES
        }
        ax: dict[str, Any] = {"role": "", "name": ""}
        for item in ax_raw.get("nodes", [])[:1]:
            if isinstance(item, dict):
                ax["role"] = _safe_text((item.get("role") or {}).get("value", ""))[:128]
                ax["name"] = _safe_text((item.get("name") or {}).get("value", ""))[:1_024]
                description = (item.get("description") or {}).get("value", "")
                if description:
                    ax["description"] = _safe_text(description)[:2_048]
                for prop in item.get("properties", []):
                    if not isinstance(prop, dict) or prop.get("name") not in {
                        "disabled",
                        "focused",
                        "expanded",
                        "checked",
                    }:
                        continue
                    value = _as_dict(prop.get("value"))
                    candidate = value.get("value")
                    if isinstance(candidate, bool) or candidate == "mixed":
                        ax[str(prop["name"])] = candidate
        box = _quad_box((box_model.get("model") or {}).get("border"))
        visible = False
        if box is not None:
            visible = (
                box["x"] < record.viewport[0]
                and box["y"] < record.viewport[1]
                and box["x"] + box["width"] > 0
                and box["y"] + box["height"] > 0
                and computed.get("visibility") not in {"hidden", "collapse"}
                and computed.get("opacity") != "0"
            )
        return {
            "backend_node_id": backend_node_id,
            "tag": tag,
            "attributes": attrs,
            "box": box,
            "visible": visible,
            "computed": computed,
            "matched_rules": self._matched_rules(matched_raw),
            "accessibility": ax,
        }

    async def _ensure_main_document_element(
        self, record: ManagedPage, backend_node_id: int
    ) -> None:
        resolved = await record.cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = (resolved.get("object") or {}).get("objectId")
        if not isinstance(object_id, str):
            raise AgentError("element_not_found", "No inspectable element was found")
        checked = await record.cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": (
                    "function(){try{const w=this.ownerDocument&&this.ownerDocument.defaultView;"
                    "return !!w&&w===w.top;}catch(_){return false;}}"
                ),
                "returnByValue": True,
            },
        )
        if (
            checked.get("exceptionDetails") is not None
            or (checked.get("result") or {}).get("value") is not True
        ):
            raise AgentError("element_not_found", "Subframe elements are outside this snapshot")

    def _matched_rules(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for match in raw.get("matchedCSSRules", []):
            if len(result) >= MAX_MATCHED_RULES or not isinstance(match, dict):
                break
            rule = _as_dict(match.get("rule"))
            selector = _as_dict(rule.get("selectorList")).get("text", "")
            style = _as_dict(rule.get("style"))
            declarations = {
                item["name"]: _safe_text(item.get("value", ""))[:256]
                for item in style.get("cssProperties", [])
                if isinstance(item, dict) and item.get("name") in SELECTED_STYLES
            }
            if declarations:
                safe_selector = _safe_text(selector)[:2_048]
                if safe_selector:
                    result.append({"selector": safe_selector, "declarations": declarations})
        return result

    async def source_attributes(self, record: ManagedPage, backend_node_id: int) -> dict[str, str]:
        inspected = await self.inspect(record, backend_node_id=backend_node_id)
        attrs = inspected.get("attributes", {})
        return {
            name: value
            for name, value in attrs.items()
            if name in {"data-codito-source", "data-codito-host-source"}
        }

    async def act(
        self,
        record: ManagedPage,
        *,
        backend_node_id: int,
        action: str,
        text: str | None = None,
        key: str | None = None,
        option: str | None = None,
        delta_x: float = 0,
        delta_y: float = 0,
        expected_document_token: str | None = None,
    ) -> None:
        self._ensure_open(record)
        if expected_document_token is None:
            expected_document_token = self.document_token(record)
        await self._ensure_current_generation(record, expected_document_token)
        if action not in {"click", "hover", "focus", "fill", "press", "scroll", "select"}:
            raise AgentError("invalid_request", "Unsupported frontend action")
        inspected = await self.inspect(record, backend_node_id=backend_node_id)
        await self._ensure_current_generation(record, expected_document_token)
        tag = inspected["tag"]
        if action == "fill":
            if text is None:
                raise AgentError("invalid_request", "Fill requires text")
        if tag == "input" and action in {"click", "focus", "press"}:
            await self._ensure_not_file_input(record, backend_node_id, expected_document_token)
        box = inspected.get("box")
        if action in {"click", "hover"} and not isinstance(box, dict):
            raise AgentError("element_not_interactable", "The element has no visible box")
        pointer_box = cast(dict[str, float], box)
        side_effect_started = False
        try:
            if action == "click":
                record.blocked_navigation_event.clear()
                side_effect_started = True
                await self._mouse(
                    record,
                    backend_node_id,
                    pointer_box,
                    click=True,
                    expected_document_token=expected_document_token,
                )
                try:
                    await asyncio.wait_for(record.blocked_navigation_event.wait(), 0.75)
                except TimeoutError:
                    if _origin(record.page.url) != record.allowed_origin:
                        self._mark_blocked_navigation(record)
                    else:
                        return
                if record.blocked_navigation_event.is_set():
                    if record.restore_task is not None:
                        await record.restore_task
                    raise AgentError(
                        "outcome_unknown",
                        "The click attempted to leave the approved origin; the page was restored, "
                        "but earlier click side effects may have occurred",
                    )
            elif action == "hover":
                side_effect_started = True
                await self._mouse(
                    record,
                    backend_node_id,
                    pointer_box,
                    click=False,
                    expected_document_token=expected_document_token,
                )
            elif action == "focus":
                await self._ensure_current_generation(record, expected_document_token)
                side_effect_started = True
                await record.cdp.send("DOM.focus", {"backendNodeId": backend_node_id})
            elif action == "fill":
                side_effect_started = True
                await self._fill(record, backend_node_id, cast(str, text), expected_document_token)
            elif action == "press":
                if key is None or not re.fullmatch(r"[A-Za-z0-9+_-]{1,64}", key):
                    raise AgentError("invalid_request", "Keyboard key is invalid")
                side_effect_started = True
                await self._press(record, backend_node_id, key, expected_document_token)
            elif action == "scroll":
                await self._ensure_current_generation(record, expected_document_token)
                side_effect_started = True
                await self._scroll(record, backend_node_id, delta_x, delta_y)
            elif action == "select":
                if tag != "select" or option is None:
                    raise AgentError("invalid_request", "Select needs a select element and option")
                await self._ensure_current_generation(record, expected_document_token)
                side_effect_started = True
                await self._select(record, backend_node_id, option)
        except asyncio.CancelledError as exc:
            if side_effect_started:
                raise AgentError(
                    "outcome_unknown",
                    "The browser action was interrupted after dispatch; do not replay it",
                ) from exc
            raise
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError(
                "outcome_unknown",
                "The browser action result is unknown; capture a fresh snapshot before continuing",
            ) from exc

    async def _attribute(self, record: ManagedPage, backend_node_id: int, name: str) -> str:
        response = await record.cdp.send(
            "DOM.getAttributes", {"nodeId": await self._frontend_node_id(record, backend_node_id)}
        )
        attrs = response.get("attributes") if isinstance(response.get("attributes"), list) else []
        for index in range(0, len(attrs) - 1, 2):
            if str(attrs[index]).lower() == name:
                return str(attrs[index + 1])
        return ""

    async def _frontend_node_id(self, record: ManagedPage, backend_node_id: int) -> int:
        await record.cdp.send("DOM.getDocument", {"depth": 0, "pierce": True})
        response = await record.cdp.send(
            "DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": [backend_node_id]}
        )
        node_ids = response.get("nodeIds")
        if (
            not isinstance(node_ids, list)
            or not node_ids
            or not isinstance(node_ids[0], int)
            or node_ids[0] == 0
        ):
            raise AgentError("element_not_found", "The element no longer exists")
        return node_ids[0]

    async def _mouse(
        self,
        record: ManagedPage,
        backend_node_id: int,
        box: dict[str, float],
        *,
        click: bool,
        expected_document_token: str,
    ) -> None:
        left = max(0.0, float(box["x"]))
        top = max(0.0, float(box["y"]))
        right = min(float(record.viewport[0]), float(box["x"]) + float(box["width"]))
        bottom = min(float(record.viewport[1]), float(box["y"]) + float(box["height"]))
        if right <= left or bottom <= top:
            raise AgentError("element_not_interactable", "The element is outside the viewport")
        x = left + (right - left) / 2
        y = top + (bottom - top) / 2
        await self._ensure_hit_target(record, backend_node_id, x, y)
        await self._ensure_current_generation(record, expected_document_token)
        try:
            # Moving the pointer can itself fire application handlers. From this
            # boundary onward, a later stale/error result cannot safely claim
            # that no action occurred.
            await record.cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x, "y": y},
            )
            if click:
                await self._ensure_hit_target(record, backend_node_id, x, y)
                await self._ensure_current_generation(record, expected_document_token)
                await record.cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "x": x,
                        "y": y,
                        "button": "left",
                        "clickCount": 1,
                    },
                )
                await record.cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": x,
                        "y": y,
                        "button": "left",
                        "clickCount": 1,
                    },
                )
        except (asyncio.CancelledError, Exception) as exc:
            raise AgentError(
                "outcome_unknown",
                "Pointer input may have occurred; capture a fresh snapshot before continuing",
            ) from exc

    async def _press(
        self,
        record: ManagedPage,
        backend_node_id: int,
        key: str,
        expected_document_token: str,
    ) -> None:
        await self._ensure_current_generation(record, expected_document_token)
        try:
            # Focus handlers can mutate application state before key dispatch.
            await record.cdp.send("DOM.focus", {"backendNodeId": backend_node_id})
            await self._ensure_current_generation(record, expected_document_token)
            await record.page.keyboard.press(key)
        except (asyncio.CancelledError, Exception) as exc:
            raise AgentError(
                "outcome_unknown",
                "Keyboard input may have occurred; capture a fresh snapshot before continuing",
            ) from exc

    async def _fill(
        self,
        record: ManagedPage,
        backend_node_id: int,
        text: str,
        expected_document_token: str,
    ) -> None:
        resolved = await record.cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = (resolved.get("object") or {}).get("objectId")
        if not isinstance(object_id, str):
            raise AgentError("element_not_found", "The fill element no longer exists")
        await self._ensure_current_generation(record, expected_document_token)
        response = await record.cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": (
                    "function(v){const tag=(this.tagName||'').toLowerCase();"
                    "const tokens=((this.getAttribute&&this.getAttribute('autocomplete'))||'')"
                    ".slice(0,512).toLowerCase().split(/\\s+/).slice(0,32);"
                    "const blocked=['current-password','new-password','one-time-code',"
                    "'webauthn','cc-name','cc-given-name','cc-additional-name',"
                    "'cc-family-name','cc-number','cc-csc','cc-exp','cc-exp-month',"
                    "'cc-exp-year','cc-type'];"
                    "if(tokens.some(t=>blocked.includes(t)))return 'sensitive';"
                    "let metadata='';try{const clip=v=>String(v||'').slice(0,256);"
                    "const boundedText=n=>{let out='',visits=0;"
                    "const filter=this.ownerDocument.defaultView.NodeFilter.SHOW_TEXT;"
                    "const walker=this.ownerDocument.createTreeWalker(n,filter);"
                    "let text=walker.nextNode();while(text&&visits<32&&out.length<256){"
                    "if(out.length<256&&out)out+=' ';const remaining=256-out.length;"
                    "if(remaining>0)out+=text.substringData(0,remaining);visits+=1;"
                    "text=walker.nextNode();}return out.slice(0,256);};"
                    "const values=['name','id','aria-label','placeholder']"
                    ".map(a=>clip((this.getAttribute&&this.getAttribute(a))||''));"
                    "const labels=Array.prototype.slice.call(this.labels||[],0,8);"
                    "const wrapper=this.closest&&this.closest('label');"
                    "if(wrapper&&labels.length<8&&!labels.includes(wrapper))labels.push(wrapper);"
                    "const labelled=((this.getAttribute&&this.getAttribute('aria-labelledby'))"
                    "||'').slice(0,512).split(/\\s+/).filter(Boolean).slice(0,8).map(id=>"
                    "this.ownerDocument.getElementById(id)).filter(Boolean);"
                    "metadata=values.concat(labels,labelled).map(v=>typeof v==='string'?"
                    "clip(v):boundedText(v)).join(' ').slice(0,4096)"
                    ".replace(/([a-z])([A-Z])/g,'$1 $2').toLowerCase()"
                    ".replace(/[^a-z0-9]+/g,' ').slice(0,4096).trim();}"
                    "catch(_){return 'sensitive';}"
                    "const credential=/(^| )(secret|token|password|passcode|credential)( |$)|"
                    "(^| )(api|access|private|client) (key|secret)( |$)/;"
                    "if(credential.test(metadata))return 'sensitive';"
                    "if(this.matches&&this.matches(':disabled'))return 'disabled';"
                    "if(this.hasAttribute&&this.hasAttribute('readonly'))return 'readonly';"
                    "if(this.getAttribute&&this.getAttribute('aria-disabled')==='true')"
                    "return 'disabled';"
                    "if(tag==='input'){const type=(this.type||'').toLowerCase();"
                    "if(type==='password'||type==='file')return 'sensitive';"
                    "if(!['text','search','email','url','tel'].includes(type))return 'wrong-type';"
                    "if(this.readOnly)return 'readonly';this.focus();"
                    "const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"
                    "'value').set;if(typeof setter!=='function')return 'invalid';"
                    "setter.call(this,v);"
                    "if(this.value!==v)return 'invalid';}"
                    "else if(tag==='textarea'){if(this.readOnly)return 'readonly';"
                    "this.focus();const setter=Object.getOwnPropertyDescriptor("
                    "HTMLTextAreaElement.prototype,'value').set;"
                    "if(typeof setter!=='function')return 'invalid';setter.call(this,v);"
                    "if(this.value!==v)return 'invalid';}"
                    "else if(this.isContentEditable){this.focus();this.textContent=v;}"
                    "else{return 'wrong-type';}"
                    "this.dispatchEvent(new Event('input',{bubbles:true}));"
                    "this.dispatchEvent(new Event('change',{bubbles:true}));return 'filled';}"
                ),
                "arguments": [{"value": text}],
                "returnByValue": True,
            },
        )
        outcome = (response.get("result") or {}).get("value")
        if response.get("exceptionDetails") is not None:
            raise AgentError(
                "outcome_unknown",
                "The fill may have changed the element before a page error occurred",
            )
        if outcome == "sensitive":
            raise AgentError(
                "sensitive_input_blocked", "Password, secret, and file input is blocked"
            )
        if outcome in {"disabled", "readonly", "wrong-type"}:
            raise AgentError("element_not_interactable", "The element cannot be filled")
        if outcome != "filled":
            raise AgentError("outcome_unknown", "The fill outcome could not be confirmed")

    async def _ensure_not_file_input(
        self,
        record: ManagedPage,
        backend_node_id: int,
        expected_document_token: str,
    ) -> None:
        resolved = await record.cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = (resolved.get("object") or {}).get("objectId")
        if not isinstance(object_id, str):
            raise AgentError("element_not_found", "The input element no longer exists")
        await self._ensure_current_generation(record, expected_document_token)
        response = await record.cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": (
                    "function(){return !(this instanceof HTMLInputElement&&"
                    "(this.type||'').toLowerCase()==='file');}"
                ),
                "returnByValue": True,
            },
        )
        await self._ensure_current_generation(record, expected_document_token)
        if (
            response.get("exceptionDetails") is not None
            or (response.get("result") or {}).get("value") is not True
        ):
            raise AgentError(
                "sensitive_input_blocked", "File inputs cannot be focused or activated"
            )

    async def _ensure_hit_target(
        self, record: ManagedPage, backend_node_id: int, x: float, y: float
    ) -> None:
        located = await record.cdp.send(
            "DOM.getNodeForLocation",
            {"x": int(x), "y": int(y), "includeUserAgentShadowDOM": True},
        )
        hit_backend = located.get("backendNodeId")
        if hit_backend == backend_node_id:
            return
        if not isinstance(hit_backend, int):
            raise AgentError("element_not_interactable", "The element cannot receive pointer input")
        target = await record.cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        hit = await record.cdp.send("DOM.resolveNode", {"backendNodeId": hit_backend})
        target_id = (target.get("object") or {}).get("objectId")
        hit_id = (hit.get("object") or {}).get("objectId")
        if not isinstance(target_id, str) or not isinstance(hit_id, str):
            raise AgentError("element_not_interactable", "The element cannot receive pointer input")
        result = await record.cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": target_id,
                "functionDeclaration": (
                    "function(n){for(let i=0;n&&i<64;i++){"
                    "if(n===this||this.contains(n))return true;"
                    "const r=n.getRootNode&&n.getRootNode();"
                    "n=n.parentNode||(r&&r.host)||null;}return false;}"
                ),
                "arguments": [{"objectId": hit_id}],
                "returnByValue": True,
            },
        )
        value = (result.get("result") or {}).get("value")
        if value is not True or result.get("exceptionDetails") is not None:
            raise AgentError("element_not_interactable", "Another element covers this target")

    async def _scroll(
        self, record: ManagedPage, backend_node_id: int, delta_x: float, delta_y: float
    ) -> None:
        resolved = await record.cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = (resolved.get("object") or {}).get("objectId")
        if not isinstance(object_id, str):
            raise AgentError("element_not_found", "The scroll element no longer exists")
        response = await record.cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": (
                    "function(x,y){if(this===document.body||this===document.documentElement||"
                    "this===document.scrollingElement){window.scrollBy(x,y);}else{this.scrollBy(x,y);}}"
                ),
                "arguments": [
                    {"value": max(-10_000, min(10_000, delta_x))},
                    {"value": max(-10_000, min(10_000, delta_y))},
                ],
                "returnByValue": True,
            },
        )
        if response.get("exceptionDetails") is not None:
            raise AgentError(
                "outcome_unknown", "The element may have scrolled before a page error occurred"
            )

    async def _select(self, record: ManagedPage, backend_node_id: int, option: str) -> None:
        resolved = await record.cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = (resolved.get("object") or {}).get("objectId")
        if not isinstance(object_id, str):
            raise AgentError("element_not_found", "The select element no longer exists")
        # This fixed function exposes no arbitrary-evaluation surface to the caller.
        response = await record.cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": (
                    "function(v){if(!(this instanceof HTMLSelectElement))return 'wrong-type';"
                    "const options=Array.from(this.options);"
                    "let matches=options.filter(o=>o.value===v);"
                    "if(matches.length===0){matches=options.filter(o=>o.label===v||o.text.trim()===v);}"
                    "if(matches.length!==1)return matches.length?'ambiguous':'unavailable';"
                    "this.selectedIndex=matches[0].index;this.dispatchEvent(new Event('input',"
                    "{bubbles:true}));this.dispatchEvent(new Event('change',"
                    "{bubbles:true}));return 'selected';}"
                ),
                "arguments": [{"value": option}],
                "returnByValue": True,
            },
        )
        if response.get("exceptionDetails") is not None:
            raise AgentError(
                "outcome_unknown",
                "The selection may have changed before a page error occurred",
            )
        outcome = (response.get("result") or {}).get("value")
        if outcome in {"wrong-type", "ambiguous", "unavailable"}:
            raise AgentError("invalid_request", "The requested select option is unavailable")
        if outcome != "selected":
            raise AgentError("outcome_unknown", "The selection outcome could not be confirmed")

    @staticmethod
    def _layout_fingerprint(raw: dict[str, Any]) -> str | None:
        documents = raw.get("documents")
        if not isinstance(documents, list) or not documents or not isinstance(documents[0], dict):
            return None
        document = documents[0]
        nodes = document.get("nodes")
        layout = document.get("layout")
        if not isinstance(nodes, dict) or not isinstance(layout, dict):
            return None
        backend_ids = nodes.get("backendNodeId")
        node_indices = layout.get("nodeIndex")
        bounds = layout.get("bounds")
        if (
            not isinstance(backend_ids, list)
            or not isinstance(node_indices, list)
            or not isinstance(bounds, list)
        ):
            return None
        digest = hashlib.sha256()
        for position, node_index in enumerate(node_indices):
            if not isinstance(node_index, int) or not 0 <= node_index < len(backend_ids):
                continue
            box = bounds[position] if position < len(bounds) else None
            digest.update(repr((backend_ids[node_index], box)).encode("utf-8", errors="replace"))
            digest.update(b"\0")
        return digest.hexdigest()[:20]

    async def _refresh_layout_fingerprint(self, record: ManagedPage) -> None:
        raw = await record.cdp.send(
            "DOMSnapshot.captureSnapshot",
            {"computedStyles": [], "includePaintOrder": True},
        )
        fingerprint = self._layout_fingerprint(raw)
        if fingerprint is not None:
            record.layout_fingerprint = fingerprint

    async def _refresh_scroll_state(self, record: ManagedPage) -> None:
        metrics = await record.cdp.send("Page.getLayoutMetrics")
        viewport = metrics.get("visualViewport")
        if not isinstance(viewport, dict):
            viewport = metrics.get("layoutViewport")
        if not isinstance(viewport, dict):
            return
        raw_x = viewport.get("pageX")
        raw_y = viewport.get("pageY")
        if not isinstance(raw_x, (int, float)) or not isinstance(raw_y, (int, float)):
            return
        scroll_x = float(raw_x)
        scroll_y = float(raw_y)
        if not math.isfinite(scroll_x) or not math.isfinite(scroll_y):
            return
        record.scroll_x = round(scroll_x, 3)
        record.scroll_y = round(scroll_y, 3)

    async def _ensure_current_generation(self, record: ManagedPage, expected: str) -> None:
        await self._refresh_scroll_state(record)
        await self._refresh_layout_fingerprint(record)
        self._ensure_generation(record, expected)

    @staticmethod
    def document_token(record: ManagedPage) -> str:
        token = f"d{record.document_counter}:m{record.mutation_counter}"
        scroll_x = float(getattr(record, "scroll_x", 0.0))
        scroll_y = float(getattr(record, "scroll_y", 0.0))
        if scroll_x or scroll_y:
            token += f":s{scroll_x:g},{scroll_y:g}"
        layout_fingerprint = str(getattr(record, "layout_fingerprint", ""))
        if layout_fingerprint:
            token += f":l{layout_fingerprint}"
        return token

    @staticmethod
    def current_url(record: ManagedPage) -> str:
        return str(record.page.url)

    def _ensure_generation(self, record: ManagedPage, expected: str) -> None:
        if self.document_token(record) != expected:
            raise AgentError("stale_snapshot", "The page changed; capture a fresh snapshot")

    def _ensure_open(self, record: ManagedPage) -> None:
        if record.closed:
            raise AgentError("frontend_session_closed", "The frontend session is closed")
        if _origin(record.page.url) != record.allowed_origin:
            raise AgentError("navigation_blocked", "The page left its approved loopback origin")

    async def close_page(self, record: ManagedPage) -> None:
        if record.closed:
            return
        try:
            await asyncio.wait_for(record.context.close(), BROWSER_CLOSE_TIMEOUT_SECONDS)
        except Exception as exc:
            raise AgentError(
                "browser_close_failed",
                "The managed browser profile could not be closed",
                retryable=True,
            ) from exc
        record.closed = True

    async def close(self) -> None:
        pending = [task for task in self._cleanup_tasks if not task.done()]
        if pending:
            await asyncio.gather(
                *(asyncio.shield(task) for task in pending), return_exceptions=True
            )
        orphan_error: BaseException | None = None
        remaining_orphans: list[Any] = []
        for context in self._orphan_contexts:
            try:
                await self._close_unreturned_context(context)
            except BaseException as exc:
                orphan_error = orphan_error or exc
                remaining_orphans.append(context)
        self._orphan_contexts = remaining_orphans
        runtime = self._playwright
        runtime_error: BaseException | None = None
        if runtime is not None:
            try:
                await asyncio.wait_for(runtime.stop(), BROWSER_CLOSE_TIMEOUT_SECONDS)
            except BaseException as exc:
                runtime_error = exc
            else:
                self._playwright = None
        cleanup_error = orphan_error or runtime_error
        if cleanup_error is not None:
            raise AgentError(
                "browser_close_failed",
                "The managed browser runtime could not be closed",
                retryable=True,
            ) from cleanup_error
