from __future__ import annotations

import asyncio
import os
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from codito_agent import frontend_playwright
from codito_agent.errors import AgentError
from codito_agent.frontend_playwright import (
    BrowserSnapshot,
    PlaywrightAdapter,
    _is_sensitive_autocomplete,
    _safe_text,
    _safe_url,
)


def test_network_and_console_sanitizers_remove_query_credentials_and_tokens() -> None:
    assert _safe_url("https://user:pass@example.com/api?token=secret#part") == (
        "https://example.com/api"
    )
    value = _safe_text("GET https://example.com/api?token=secret token=abc password:hunter2")
    assert "secret" not in value
    assert "hunter2" not in value
    assert "?" not in value
    for unsafe_url in ("data:text/plain,secret", "blob:https://example.com/id", "file:///secret"):
        assert _safe_url(unsafe_url).startswith("/")
        assert ":" not in _safe_url(unsafe_url)
    assert _safe_url("data:text/plain,private") == "/unsupported-resource"
    assert _safe_url("blob:http://localhost/private") == "/unsupported-resource"
    assert _safe_url("file:///C:/private.txt") == "/unsupported-resource"
    assert _safe_url("chrome-extension://secret/page") == "/unsupported-resource"
    json_value = _safe_text('{"token":"abc.def","Authorization":"Bearer xyz.123"}')
    assert "abc.def" not in json_value
    assert "xyz.123" not in json_value
    for canary in (
        "Authorization: Bearer bearer.canary",
        "Authorization: Basic basic-canary",
        "Authorization: Digest digest-canary, nonce=secret",
        "Cookie: session=cookie-canary; theme=dark",
        "Set-Cookie: session=set-cookie-canary; HttpOnly",
        '{"password":"password-canary","token":"token-canary"}',
    ):
        sanitized = _safe_text(canary)
        assert "canary" not in sanitized
    for local_path in (
        r"D:\Workspaces\private\src.tsx",
        r"\\build-server\private-share\token.txt",
        r"C:\Users\Jane Doe\private\App.tsx:10",
        r"\\build-server\private share\token.txt",
        "file:///C:/Users/private/token.txt",
    ):
        sanitized = _safe_text(f"failed at {local_path}")
        assert "private" not in sanitized
        assert "[local path redacted]" in sanitized


def test_sensitive_autocomplete_recognizes_section_and_billing_token_lists() -> None:
    assert _is_sensitive_autocomplete("section-login current-password")
    assert _is_sensitive_autocomplete("billing cc-number")
    assert _is_sensitive_autocomplete("section-payment cc-exp-year")
    assert _is_sensitive_autocomplete("username webauthn")
    assert not _is_sensitive_autocomplete("section-login username")


@pytest.mark.asyncio
async def test_frozen_runtime_forces_packaged_browser_path(monkeypatch, tmp_path: Path) -> None:
    adapter = PlaywrightAdapter()
    monkeypatch.setattr(frontend_playwright.sys, "frozen", True, raising=False)
    executable = tmp_path / "daemon" / "Codito.Agent.Daemon.exe"
    monkeypatch.setattr(frontend_playwright.sys, "executable", str(executable))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "C:/attacker")
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "C:/attacker/node.exe")

    class Runtime:
        async def start(self):
            return object()

    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        type("Module", (), {"async_playwright": staticmethod(lambda: Runtime())})(),
    )
    await adapter._load()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "browsers")
    assert "PLAYWRIGHT_NODEJS_PATH" not in os.environ


@pytest.mark.asyncio
async def test_open_enables_chromium_sandbox_materializes_final_dom_and_closes_extra_pages(
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    class Page:
        def __init__(self, url: str) -> None:
            self.url = url
            self.main_frame = object()
            self.closed = False

        def on(self, _event, _callback) -> None:
            return None

        async def goto(self, url: str, **_kwargs) -> None:
            self.url = url

        async def close(self) -> None:
            self.closed = True

    class Cdp:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, method: str, _params=None):
            self.sent.append(method)
            return {}

        def on(self, _event, _callback) -> None:
            return None

    restored_main = Page("https://stale.example/private")
    extra = Page("https://example.com/restored")
    fresh = Page("about:blank")
    cdp = Cdp()

    class Context:
        def __init__(self) -> None:
            self.pages = [restored_main, extra]

        async def route(self, _pattern, _callback) -> None:
            return None

        def on(self, _event, _callback) -> None:
            return None

        async def new_page(self):
            return fresh

        async def new_cdp_session(self, _page):
            return cdp

        async def close(self) -> None:
            return None

    class Chromium:
        async def launch_persistent_context(self, _profile: str, **kwargs):
            calls.update(kwargs)
            return Context()

    adapter = PlaywrightAdapter(fallback_channel=None)
    adapter._playwright = SimpleNamespace(chromium=Chromium())
    record = await adapter.open(
        profile_directory=tmp_path / "profile",
        url="http://localhost:5173/dashboard",
        allowed_origin="http://localhost:5173",
        viewport=(320, 240),
    )

    assert calls["chromium_sandbox"] is True
    assert calls["timeout"] == 30_000
    assert record.page is fresh
    assert restored_main.closed
    assert extra.closed
    assert cdp.sent.count("DOM.getDocument") == 2
    await adapter.close_page(record)


@pytest.mark.asyncio
async def test_cancelled_driver_start_closes_the_unreturned_playwright_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()

    class Manager:
        closed = False

        async def start(self):
            start_entered.set()
            await asyncio.Event().wait()

        async def __aexit__(self, *_args) -> None:
            self.closed = True

    manager = Manager()
    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        SimpleNamespace(async_playwright=lambda: manager),
    )
    adapter = PlaywrightAdapter()

    task = asyncio.create_task(adapter._load())
    await asyncio.wait_for(start_entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.closed
    assert not adapter._cleanup_tasks


@pytest.mark.asyncio
async def test_cancelled_context_launch_closes_context_returned_after_cancellation(
    tmp_path: Path,
) -> None:
    launch_entered = asyncio.Event()
    allow_launch = asyncio.Event()

    class Context:
        closed = False

        async def close(self) -> None:
            self.closed = True

    context = Context()

    class Chromium:
        async def launch_persistent_context(self, _profile: str, **_kwargs):
            launch_entered.set()
            await allow_launch.wait()
            return context

    adapter = PlaywrightAdapter(fallback_channel=None)
    adapter._playwright = SimpleNamespace(chromium=Chromium())
    task = asyncio.create_task(
        adapter.open(
            profile_directory=tmp_path / "profile",
            url="http://localhost:5173/",
            allowed_origin="http://localhost:5173",
            viewport=(320, 240),
        )
    )
    await asyncio.wait_for(launch_entered.wait(), 1)
    task.cancel()
    allow_launch.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert context.closed
    assert not adapter._cleanup_tasks


@pytest.mark.asyncio
async def test_cancelled_open_setup_closes_launched_unreturned_context(tmp_path: Path) -> None:
    setup_entered = asyncio.Event()

    class Context:
        def __init__(self) -> None:
            self.pages: list[object] = []
            self.closed = False

        async def route(self, _pattern, _callback) -> None:
            setup_entered.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    context = Context()

    class Chromium:
        async def launch_persistent_context(self, _profile: str, **_kwargs):
            return context

    adapter = PlaywrightAdapter(fallback_channel=None)
    adapter._playwright = SimpleNamespace(chromium=Chromium())
    task = asyncio.create_task(
        adapter.open(
            profile_directory=tmp_path / "profile",
            url="http://localhost:5173/",
            allowed_origin="http://localhost:5173",
            viewport=(320, 240),
        )
    )
    await asyncio.wait_for(setup_entered.wait(), 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert context.closed
    assert not adapter._cleanup_tasks


@pytest.mark.asyncio
async def test_second_cancellation_quarantines_a_context_if_close_later_fails(
    tmp_path: Path,
) -> None:
    setup_entered = asyncio.Event()
    close_entered = asyncio.Event()
    finish_close = asyncio.Event()

    class Context:
        def __init__(self) -> None:
            self.pages: list[object] = []
            self.fail_close = True

        async def route(self, _pattern, _callback) -> None:
            setup_entered.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            close_entered.set()
            await finish_close.wait()
            if self.fail_close:
                raise OSError("close failed")

    context = Context()

    class Chromium:
        async def launch_persistent_context(self, _profile: str, **_kwargs):
            return context

    adapter = PlaywrightAdapter(fallback_channel=None)
    adapter._playwright = SimpleNamespace(chromium=Chromium())
    opening = asyncio.create_task(
        adapter.open(
            profile_directory=tmp_path / "profile",
            url="http://localhost:5173/",
            allowed_origin="http://localhost:5173",
            viewport=(320, 240),
        )
    )
    await asyncio.wait_for(setup_entered.wait(), 1)
    opening.cancel()
    await asyncio.wait_for(close_entered.wait(), 1)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    finish_close.set()
    for _ in range(20):
        if adapter._orphan_contexts:
            break
        await asyncio.sleep(0)

    assert adapter._orphan_contexts == [context]
    context.fail_close = False
    adapter._playwright = None
    await adapter.close()
    assert not adapter._orphan_contexts


def test_semantic_tree_uses_viewport_bounds_nested_text_and_returned_parents() -> None:
    adapter = PlaywrightAdapter()
    strings = ["#document", "DIV", "#text", "Hello", "STRONG", "world", ""]
    raw = {
        "strings": strings,
        "documents": [
            {
                "scrollOffsetX": 0,
                "scrollOffsetY": 806,
                "nodes": {
                    "backendNodeId": [1, 10, 20, 11, 21],
                    "nodeName": [0, 1, 2, 4, 2],
                    "nodeValue": [6, 6, 3, 6, 5],
                    "nodeType": [9, 1, 3, 1, 3],
                    "attributes": [[], [], [], [], []],
                    "parentIndex": [-1, 0, 1, 1, 3],
                },
                "layout": {
                    "nodeIndex": [1, 2, 3, 4],
                    "bounds": [
                        [10, 1008, 180, 30],
                        [10, 1008, 50, 30],
                        [70, 1008, 80, 30],
                        [70, 1008, 60, 30],
                    ],
                    "styles": [[], [], [], []],
                },
            }
        ],
    }

    elements, truncated = adapter._semantic_elements(raw, {"nodes": []}, 500, (320, 240))

    assert not truncated
    assert [item["tag"] for item in elements] == ["div", "strong"]
    assert elements[0]["box"]["y"] == 202
    assert elements[0]["text"] == "Hello"
    assert elements[1]["text"] == "world"
    assert elements[1]["parent_backend_node_id"] == 10

    limited, limited_truncated = adapter._semantic_elements(raw, {"nodes": []}, 1, (320, 240))
    assert limited_truncated
    assert limited[0]["parent_backend_node_id"] is None


def test_semantic_tree_prioritizes_late_actionable_nodes_over_wrapper_cap() -> None:
    adapter = PlaywrightAdapter()
    wrapper_count = 501
    button_index = wrapper_count + 1
    raw = {
        "strings": ["#document", "DIV", "BUTTON", ""],
        "documents": [
            {
                "nodes": {
                    "backendNodeId": list(range(button_index + 1)),
                    "nodeName": [0] + [1] * wrapper_count + [2],
                    "nodeValue": [3] * (button_index + 1),
                    "nodeType": [9] + [1] * (button_index),
                    "attributes": [[] for _ in range(button_index + 1)],
                    "parentIndex": [-1, *range(button_index)],
                },
                "layout": {
                    "nodeIndex": list(range(1, button_index + 1)),
                    "bounds": [[0, 0, 100, 30] for _ in range(button_index)],
                    "styles": [[] for _ in range(button_index)],
                },
            }
        ],
    }

    ax = {
        "nodes": [
            {
                "backendDOMNodeId": backend_id,
                "role": {"value": "generic"},
                "name": {"value": ""},
            }
            for backend_id in range(1, wrapper_count + 1)
        ]
    }
    elements, truncated = adapter._semantic_elements(raw, ax, 500, (320, 240))

    assert truncated
    assert len(elements) == 500
    button = next(item for item in elements if item["tag"] == "button")
    assert button["backend_node_id"] == button_index
    assert button["parent_backend_node_id"] == wrapper_count


class InspectCdp:
    def __init__(
        self,
        *,
        select_result: str = "selected",
        main_document: bool = True,
        runtime_exception: bool = False,
    ) -> None:
        self.select_result = select_result
        self.main_document = main_document
        self.runtime_exception = runtime_exception
        self.scroll_y = 0.0
        self.calls: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None):
        params = params or {}
        self.calls.append((method, params))
        if method == "Page.getLayoutMetrics":
            return {"visualViewport": {"pageX": 0, "pageY": self.scroll_y}}
        if method == "DOMSnapshot.captureSnapshot":
            return {"documents": []}
        if method == "DOM.getNodeForLocation":
            return {"backendNodeId": 22}
        if method == "DOM.getDocument":
            return {}
        if method == "DOM.pushNodesByBackendIdsToFrontend":
            return {"nodeIds": [2]}
        if method == "DOM.describeNode" and params["nodeId"] == 2:
            return {
                "node": {
                    "nodeType": 3,
                    "nodeName": "#text",
                    "backendNodeId": 22,
                    "parentId": 1,
                }
            }
        if method == "DOM.describeNode":
            return {
                "node": {
                    "nodeType": 1,
                    "nodeName": "BUTTON",
                    "backendNodeId": 11,
                    "attributes": [],
                }
            }
        if method == "DOM.getBoxModel":
            return {"model": {"border": [0, 0, 80, 0, 80, 30, 0, 30]}}
        if method == "CSS.getComputedStyleForNode":
            return {"computedStyle": []}
        if method == "CSS.getMatchedStylesForNode":
            return {"matchedCSSRules": []}
        if method == "Accessibility.getPartialAXTree":
            return {"nodes": []}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "select-object"}}
        if method == "Runtime.callFunctionOn":
            if "arguments" not in params:
                return {"result": {"value": self.main_document}}
            if self.runtime_exception:
                return {"exceptionDetails": {"text": "after mutation"}}
            return {"result": {"value": self.select_result}}
        raise AssertionError(method)


def managed_record(cdp) -> SimpleNamespace:
    return SimpleNamespace(
        cdp=cdp,
        page=SimpleNamespace(url="http://localhost:5173/dashboard"),
        allowed_origin="http://localhost:5173",
        viewport=(320, 240),
        document_counter=0,
        mutation_counter=0,
        closed=False,
    )


@pytest.mark.asyncio
async def test_coordinate_inspect_normalizes_text_hit_to_parent_element() -> None:
    adapter = PlaywrightAdapter()
    inspected = await adapter.inspect(managed_record(InspectCdp()), x=10, y=10)
    assert inspected["backend_node_id"] == 11
    assert inspected["tag"] == "button"

    with pytest.raises(AgentError, match="Subframe"):
        await adapter.inspect(managed_record(InspectCdp(main_document=False)), x=10, y=10)


@pytest.mark.asyncio
async def test_manual_main_frame_scroll_invalidates_snapshot_before_action() -> None:
    adapter = PlaywrightAdapter()
    cdp = InspectCdp()
    record = managed_record(cdp)
    generation = adapter.document_token(record)
    cdp.scroll_y = 806

    with pytest.raises(AgentError, match="page changed") as error:
        await adapter.act(
            record,
            backend_node_id=11,
            action="hover",
            expected_document_token=generation,
        )

    assert error.value.code == "stale_snapshot"
    assert adapter.document_token(record).endswith(":s0,806")
    assert all(method != "DOM.scrollIntoViewIfNeeded" for method, _params in cdp.calls)


@pytest.mark.asyncio
async def test_nested_layout_scroll_fingerprint_invalidates_snapshot() -> None:
    adapter = PlaywrightAdapter()

    class LayoutCdp(InspectCdp):
        nested_y = 20

        async def send(self, method: str, params: dict | None = None):
            if method == "DOMSnapshot.captureSnapshot":
                return {
                    "documents": [
                        {
                            "nodes": {"backendNodeId": [1, 11]},
                            "layout": {
                                "nodeIndex": [1],
                                "bounds": [[0, self.nested_y, 80, 30]],
                            },
                        }
                    ]
                }
            return await super().send(method, params)

    cdp = LayoutCdp()
    record = managed_record(cdp)
    await adapter._refresh_layout_fingerprint(record)
    generation = adapter.document_token(record)
    cdp.nested_y = -120

    with pytest.raises(AgentError, match="page changed"):
        await adapter.act(
            record,
            backend_node_id=11,
            action="hover",
            expected_document_token=generation,
        )

    assert adapter.document_token(record) != generation


@pytest.mark.asyncio
async def test_select_requires_one_matching_value_or_visible_label() -> None:
    adapter = PlaywrightAdapter()
    selected = InspectCdp(select_result="selected")
    await adapter._select(managed_record(selected), 11, "Visible option")
    call = next(params for method, params in selected.calls if method == "Runtime.callFunctionOn")
    assert call["arguments"] == [{"value": "Visible option"}]
    assert "o.label===v" in call["functionDeclaration"]

    with pytest.raises(AgentError, match="unavailable"):
        await adapter._select(managed_record(InspectCdp(select_result="ambiguous")), 11, "Same")


@pytest.mark.asyncio
async def test_fill_checks_sensitive_attributes_atomically_with_value_update() -> None:
    adapter = PlaywrightAdapter()
    filled = InspectCdp(select_result="filled")
    await adapter._fill(managed_record(filled), 11, "public text", "d0:m0")
    call = next(params for method, params in filled.calls if method == "Runtime.callFunctionOn")
    assert "current-password" in call["functionDeclaration"]
    assert "cc-exp-year" in call["functionDeclaration"]
    assert call["functionDeclaration"].index("getAttribute('autocomplete')") < call[
        "functionDeclaration"
    ].index("if(tag==='input')")
    assert "'name','id','aria-label','placeholder'" in call["functionDeclaration"]
    assert "this.labels" in call["functionDeclaration"]
    assert "String(v||'').slice(0,256)" in call["functionDeclaration"]
    assert "slice.call(this.labels||[],0,8)" in call["functionDeclaration"]
    assert "filter(Boolean).slice(0,8)" in call["functionDeclaration"]
    assert "join(' ').slice(0,4096)" in call["functionDeclaration"]
    assert "createTreeWalker" in call["functionDeclaration"]
    assert "visits<32" in call["functionDeclaration"]
    assert "substringData(0,remaining)" in call["functionDeclaration"]
    assert "innerText" not in call["functionDeclaration"]
    assert "boundedText(v)" in call["functionDeclaration"]
    assert "v.textContent" not in call["functionDeclaration"]
    assert "secret|token|password|passcode|credential" in call["functionDeclaration"]
    assert "['text','search','email','url','tel']" in call["functionDeclaration"]
    assert ":disabled" in call["functionDeclaration"]
    assert "readOnly" in call["functionDeclaration"]
    assert "HTMLInputElement.prototype" in call["functionDeclaration"]
    assert "HTMLTextAreaElement.prototype" in call["functionDeclaration"]
    assert "setter.call(this,v)" in call["functionDeclaration"]
    assert "this.value=v" not in call["functionDeclaration"]

    with pytest.raises(AgentError, match="Password, secret"):
        await adapter._fill(
            managed_record(InspectCdp(select_result="sensitive")),
            11,
            "blocked",
            "d0:m0",
        )

    for outcome in ("disabled", "readonly", "wrong-type"):
        with pytest.raises(AgentError, match="cannot be filled"):
            await adapter._fill(
                managed_record(InspectCdp(select_result=outcome)),
                11,
                "blocked",
                "d0:m0",
            )


@pytest.mark.asyncio
async def test_mutation_then_renderer_error_is_never_reported_as_definitive() -> None:
    adapter = PlaywrightAdapter()
    for operation in ("fill", "select", "scroll"):
        cdp = InspectCdp(runtime_exception=True)
        record = managed_record(cdp)
        with pytest.raises(AgentError) as error:
            if operation == "fill":
                await adapter._fill(record, 11, "changed", "d0:m0")
            elif operation == "select":
                await adapter._select(record, 11, "Changed")
            else:
                await adapter._scroll(record, 11, 0, 100)
        assert error.value.code == "outcome_unknown"


@pytest.mark.asyncio
async def test_file_input_cannot_be_activated_and_chooser_is_cleared() -> None:
    adapter = PlaywrightAdapter()
    with pytest.raises(AgentError, match="File inputs"):
        await adapter._ensure_not_file_input(
            managed_record(InspectCdp(main_document=False)), 11, "d0:m0"
        )

    class Chooser:
        values = None

        async def set_files(self, values) -> None:
            self.values = values

    chooser = Chooser()
    await adapter._clear_file_chooser(chooser)
    assert chooser.values == []


@pytest.mark.asyncio
async def test_pointer_hit_target_walks_across_open_shadow_hosts() -> None:
    class Cdp:
        function = ""

        async def send(self, method: str, params=None):
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": 22}
            if method == "DOM.resolveNode":
                return {"object": {"objectId": f"node-{params['backendNodeId']}"}}
            if method == "Runtime.callFunctionOn":
                self.function = params["functionDeclaration"]
                return {"result": {"value": True}}
            raise AssertionError(method)

    cdp = Cdp()
    await PlaywrightAdapter()._ensure_hit_target(managed_record(cdp), 11, 10, 10)
    assert "r&&r.host" in cdp.function


@pytest.mark.asyncio
async def test_committed_about_navigation_is_restored_and_click_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = PlaywrightAdapter()
    frame = SimpleNamespace(url="http://localhost:5173/dashboard")

    class Page:
        url = frame.url
        main_frame = frame

        async def goto(self, url: str, **_options) -> None:
            self.url = url
            frame.url = url

    record = SimpleNamespace(
        cdp=SimpleNamespace(send=lambda *_args, **_kwargs: None),
        page=Page(),
        allowed_origin="http://localhost:5173",
        viewport=(320, 240),
        document_counter=0,
        mutation_counter=0,
        blocked_navigation=False,
        blocked_navigation_event=asyncio.Event(),
        restore_task=None,
        navigation_lock=asyncio.Lock(),
        last_safe_url="http://localhost:5173/dashboard",
        closed=False,
    )

    async def cdp_send(_method, _params=None):
        return {}

    record.cdp.send = cdp_send

    async def inspect(_record, **_kwargs):
        return {
            "backend_node_id": 11,
            "tag": "a",
            "box": {"x": 0, "y": 0, "width": 80, "height": 30},
        }

    async def navigate_on_click(_record, *_args, **_kwargs):
        record.page.url = "about:blank"
        frame.url = "about:blank"
        adapter._navigated(record, frame)

    monkeypatch.setattr(adapter, "inspect", inspect)
    monkeypatch.setattr(adapter, "_mouse", navigate_on_click)
    generation = "d0:m0"

    with pytest.raises(AgentError) as error:
        await adapter.act(
            record,
            backend_node_id=11,
            action="click",
            expected_document_token=generation,
        )
    assert error.value.code == "outcome_unknown"
    assert record.page.url == "http://localhost:5173/dashboard"


@pytest.mark.asyncio
async def test_click_cancellation_after_dispatch_is_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = PlaywrightAdapter()
    dispatched = asyncio.Event()
    record = SimpleNamespace(
        page=SimpleNamespace(url="http://localhost:5173/dashboard"),
        allowed_origin="http://localhost:5173",
        viewport=(320, 240),
        blocked_navigation_event=asyncio.Event(),
        restore_task=None,
        document_counter=0,
        mutation_counter=0,
        scroll_x=0.0,
        scroll_y=0.0,
        layout_fingerprint="",
        closed=False,
    )

    async def generation_check(*_args, **_kwargs) -> None:
        return None

    async def inspect(*_args, **_kwargs):
        return {
            "tag": "button",
            "box": {"x": 0.0, "y": 0.0, "width": 80.0, "height": 30.0},
        }

    async def mouse(*_args, **_kwargs) -> None:
        dispatched.set()

    monkeypatch.setattr(adapter, "_ensure_current_generation", generation_check)
    monkeypatch.setattr(adapter, "inspect", inspect)
    monkeypatch.setattr(adapter, "_mouse", mouse)
    task = asyncio.create_task(
        adapter.act(
            record,
            backend_node_id=11,
            action="click",
            expected_document_token=adapter.document_token(record),
        )
    )
    await asyncio.wait_for(dispatched.wait(), 1)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(AgentError) as error:
        await task
    assert error.value.code == "outcome_unknown"


@pytest.mark.asyncio
async def test_irreversible_pointer_and_focus_mutations_report_unknown() -> None:
    adapter = PlaywrightAdapter()

    class PointerCdp:
        record = None

        async def send(self, method: str, params=None):
            if method == "Page.getLayoutMetrics":
                return {"visualViewport": {"pageX": 0, "pageY": 0}}
            if method == "DOMSnapshot.captureSnapshot":
                return {"documents": []}
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": 11}
            if method == "Input.dispatchMouseEvent":
                if params["type"] == "mouseMoved":
                    self.record.mutation_counter += 1
                return {}
            if method == "DOM.focus":
                self.record.mutation_counter += 1
                return {}
            raise AssertionError(method)

    cdp = PointerCdp()
    record = managed_record(cdp)
    record.viewport = (320, 240)
    record.page.keyboard = SimpleNamespace(press=lambda _key: None)
    cdp.record = record
    box = {"x": 0, "y": 0, "width": 80, "height": 30}
    generation = "d0:m0"

    with pytest.raises(AgentError) as pointer_error:
        await adapter._mouse(
            record,
            11,
            box,
            click=True,
            expected_document_token=generation,
        )
    assert pointer_error.value.code == "outcome_unknown"

    record.mutation_counter = 0
    with pytest.raises(AgentError) as press_error:
        await adapter._press(record, 11, "Enter", generation)
    assert press_error.value.code == "outcome_unknown"


class SnapshotPage:
    def __init__(self, record: SimpleNamespace, *, always_mutate: bool = False) -> None:
        self.url = "http://localhost:5173/dashboard?token=private"
        self.record = record
        self.always_mutate = always_mutate
        self.calls = 0

    async def screenshot(self, **_kwargs) -> bytes:
        self.calls += 1
        if self.always_mutate or self.calls == 1:
            self.record.mutation_counter += 1
        return b"png"

    async def title(self) -> str:
        return "Dashboard"


class SnapshotCdp:
    def __init__(self, *, extra_document: bool = False) -> None:
        self.materialized = 0
        self.extra_document = extra_document

    async def send(self, method: str, _params: dict | None = None):
        if method == "Page.getLayoutMetrics":
            return {"visualViewport": {"pageX": 0, "pageY": 0}}
        if method == "DOM.getDocument":
            self.materialized += 1
            return {}
        if method == "DOMSnapshot.captureSnapshot":
            documents = [{}, {}] if self.extra_document else []
            return {"strings": [], "documents": documents}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": []}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_snapshot_retries_mutation_and_returns_coherent_generation() -> None:
    adapter = PlaywrightAdapter()
    cdp = SnapshotCdp(extra_document=True)
    record = SimpleNamespace(
        cdp=cdp,
        page=None,
        allowed_origin="http://localhost:5173",
        viewport=(320, 240),
        console=[],
        network=[],
        document_counter=1,
        mutation_counter=0,
        closed=False,
    )
    record.page = SnapshotPage(record)

    result = await adapter.snapshot(record, max_elements=500)

    assert isinstance(result, BrowserSnapshot)
    assert result.document_token == f"d{1}:m{1}"
    assert result.url == "http://localhost:5173/dashboard"
    assert record.page.calls == 2
    assert cdp.materialized == 2
    assert result.warnings == ["Subframe documents are excluded from this snapshot."]

    changing = SimpleNamespace(**vars(record))
    changing.mutation_counter = 0
    changing.page = SnapshotPage(changing, always_mutate=True)
    with pytest.raises(AgentError, match="kept changing"):
        await adapter.snapshot(changing, max_elements=500)


@pytest.mark.asyncio
async def test_snapshot_keeps_failures_behind_successes_then_drains_them() -> None:
    adapter = PlaywrightAdapter()
    cdp = SnapshotCdp()
    record = SimpleNamespace(
        cdp=cdp,
        page=None,
        allowed_origin="http://localhost:5173",
        viewport=(320, 240),
        console=deque(),
        network=deque(maxlen=100),
        document_counter=1,
        mutation_counter=0,
        closed=False,
    )
    record.page = SnapshotPage(record)
    adapter._response(
        record,
        SimpleNamespace(
            ok=False,
            status=500,
            url="http://localhost:5173/fail?token=private",
            request=SimpleNamespace(method="GET"),
        ),
    )
    for index in range(25):
        adapter._response(
            record,
            SimpleNamespace(
                ok=True,
                status=200,
                url=f"http://localhost:5173/ok/{index}",
                request=SimpleNamespace(method="GET"),
            ),
        )

    first = await adapter.snapshot(record, max_elements=500)
    second = await adapter.snapshot(record, max_elements=500)

    assert first.network == [
        {
            "method": "GET",
            "url": "http://localhost:5173/fail",
            "status": 500,
            "ok": False,
        }
    ]
    assert second.network == []


@pytest.mark.asyncio
async def test_failed_context_close_stays_retryable() -> None:
    class Context:
        fail = True

        async def close(self) -> None:
            if self.fail:
                raise OSError("still open")

    context = Context()
    record = SimpleNamespace(context=context, closed=False)
    adapter = PlaywrightAdapter()
    with pytest.raises(AgentError, match="could not be closed"):
        await adapter.close_page(record)
    assert not record.closed
    context.fail = False
    await adapter.close_page(record)
    assert record.closed


@pytest.mark.asyncio
async def test_adapter_shutdown_bounds_a_stuck_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        blocked = True

        async def stop(self) -> None:
            if self.blocked:
                await asyncio.Event().wait()

    monkeypatch.setattr(frontend_playwright, "BROWSER_CLOSE_TIMEOUT_SECONDS", 0.01)
    adapter = PlaywrightAdapter()
    runtime = Runtime()
    adapter._playwright = runtime

    with pytest.raises(AgentError) as error:
        await adapter.close()
    assert error.value.code == "browser_close_failed"
    assert adapter._playwright is runtime
    runtime.blocked = False
    await adapter.close()
    assert adapter._playwright is None
