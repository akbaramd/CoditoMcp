from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from codito_agent.errors import AgentError
from codito_agent.frontend_playwright import PlaywrightAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("CODITO_REAL_BROWSER_TESTS") != "1",
    reason="set CODITO_REAL_BROWSER_TESTS=1 to run the installed Edge regression",
)

PAGE = b"""<!doctype html>
<meta charset=utf-8><title>Codito browser boundary</title>
<a id=leave href=about:blank>Leave origin</a>
<input id=disabled-input disabled value=before>
<input id=readonly-input readonly value=before>
<input id=number-input type=number value=1>
<input id=password-input type=password>
<input id=card-input autocomplete='section-payment cc-exp-year'>
<textarea id=otp-textarea autocomplete='one-time-code'></textarea>
<div id=password-editor contenteditable autocomplete='current-password'>edit</div>
<label for=api-key-input>API key</label><input id=api-key-input>
<textarea id=token-textarea name=release_token></textarea>
<div id=private-key-editor contenteditable placeholder='Private key'>edit</div>
<input id=tokenizer-setting value=before>
<input id=file-input type=file>
<div id=shadow-host></div>
<iframe srcdoc='<button id=framed-button>Framed secret</button>'></iframe>
<div style='height:1600px'></div><button id=bottom-button>Bottom</button>
<script>
const controlled = document.querySelector('#tokenizer-setting');
const nativeInputValue = Object.getOwnPropertyDescriptor(
  HTMLInputElement.prototype, 'value');
let frameworkValue = controlled.value;
Object.defineProperty(controlled, 'value', {
  configurable: true,
  get() { return nativeInputValue.get.call(this); },
  set(value) {
    nativeInputValue.set.call(this, value);
    frameworkValue = String(value);
  }
});
window.frameworkChanges = 0;
window.frameworkValue = frameworkValue;
controlled.addEventListener('input', () => {
  const next = nativeInputValue.get.call(controlled);
  if (next !== frameworkValue) {
    frameworkValue = next;
    window.frameworkValue = next;
    window.frameworkChanges += 1;
  }
});
const root = document.querySelector('#shadow-host').attachShadow({mode:'open'});
root.innerHTML = `<button id="shadow-button"
 data-codito-source="src/App.tsx:1:1"
 onclick="this.textContent='Clicked shadow'">Shadow action</button>
 <div id="shadow-scroller" style="height:40px;overflow:auto">
   <div style="height:400px">Nested shadow content</div>
 </div>`;
</script>
"""


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(PAGE)}\r\nConnection: close\r\n\r\n".encode()
            + PAGE
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_real_edge_shadow_frame_fill_and_about_navigation_boundaries(
    tmp_path: Path,
) -> None:
    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    adapter = PlaywrightAdapter(visible=False, fallback_channel="msedge")
    record = None
    try:
        record = await adapter.open(
            profile_directory=tmp_path / "edge-profile",
            url=f"{origin}/app",
            allowed_origin=origin,
            viewport=(800, 600),
        )
        snapshot = await adapter.snapshot(record, max_elements=500)
        assert snapshot.warnings == ["Subframe documents are excluded from this snapshot."]
        assert all("Framed secret" not in element["text"] for element in snapshot.elements)

        shadow = next(
            element
            for element in snapshot.elements
            if element["attributes"].get("id") == "shadow-button"
        )
        inspected = await adapter.inspect(record, backend_node_id=shadow["backend_node_id"])
        assert inspected["tag"] == "button"
        assert await adapter.source_attributes(record, shadow["backend_node_id"]) == {
            "data-codito-source": "src/App.tsx:1:1"
        }
        await record.page.locator("#shadow-host").evaluate(
            "el => { el.shadowRoot.querySelector('#shadow-scroller').scrollTop = 120; }"
        )
        with pytest.raises(AgentError) as nested_scroll_error:
            await adapter.act(
                record,
                backend_node_id=shadow["backend_node_id"],
                action="hover",
                expected_document_token=snapshot.document_token,
            )
        assert nested_scroll_error.value.code == "stale_snapshot"

        snapshot = await adapter.snapshot(record, max_elements=500)
        shadow = next(
            element
            for element in snapshot.elements
            if element["attributes"].get("id") == "shadow-button"
        )
        await adapter.act(
            record,
            backend_node_id=shadow["backend_node_id"],
            action="click",
            expected_document_token=snapshot.document_token,
        )

        snapshot = await adapter.snapshot(record, max_elements=500)
        by_id = {
            element["attributes"].get("id"): element
            for element in snapshot.elements
            if element["attributes"].get("id")
        }
        for element_id, expected_code in (
            ("disabled-input", "element_not_interactable"),
            ("readonly-input", "element_not_interactable"),
            ("number-input", "element_not_interactable"),
            ("password-input", "sensitive_input_blocked"),
            ("card-input", "sensitive_input_blocked"),
            ("otp-textarea", "sensitive_input_blocked"),
            ("password-editor", "sensitive_input_blocked"),
            ("api-key-input", "sensitive_input_blocked"),
            ("token-textarea", "sensitive_input_blocked"),
            ("private-key-editor", "sensitive_input_blocked"),
        ):
            with pytest.raises(AgentError) as fill_error:
                await adapter.act(
                    record,
                    backend_node_id=by_id[element_id]["backend_node_id"],
                    action="fill",
                    text="must-not-apply",
                    expected_document_token=snapshot.document_token,
                )
            assert fill_error.value.code == expected_code

        with pytest.raises(AgentError) as file_error:
            await adapter.act(
                record,
                backend_node_id=by_id["file-input"]["backend_node_id"],
                action="click",
                expected_document_token=snapshot.document_token,
            )
        assert file_error.value.code == "sensitive_input_blocked"

        with pytest.raises(AgentError) as navigation_error:
            await adapter.act(
                record,
                backend_node_id=by_id["leave"]["backend_node_id"],
                action="click",
                expected_document_token=snapshot.document_token,
            )
        assert navigation_error.value.code == "outcome_unknown"
        assert urlsplit(record.page.url).scheme == "http"
        assert f"{urlsplit(record.page.url).hostname}:{urlsplit(record.page.url).port}" == (
            f"127.0.0.1:{port}"
        )

        before_scroll = await adapter.snapshot(record, max_elements=500)
        leave = next(
            element
            for element in before_scroll.elements
            if element["attributes"].get("id") == "leave"
        )
        await record.page.mouse.wheel(0, 1_200)
        await asyncio.sleep(0.1)
        with pytest.raises(AgentError) as scroll_error:
            await adapter.act(
                record,
                backend_node_id=leave["backend_node_id"],
                action="hover",
                expected_document_token=before_scroll.document_token,
            )
        assert scroll_error.value.code == "stale_snapshot"

        await record.page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.1)
        controlled_snapshot = await adapter.snapshot(record, max_elements=500)
        controlled = next(
            element
            for element in controlled_snapshot.elements
            if element["attributes"].get("id") == "tokenizer-setting"
        )
        await adapter.act(
            record,
            backend_node_id=controlled["backend_node_id"],
            action="fill",
            text="after",
            expected_document_token=controlled_snapshot.document_token,
        )
        framework_state = await record.page.locator("#tokenizer-setting").evaluate(
            "el => ({value: el.value, changes: window.frameworkChanges, "
            "frameworkValue: window.frameworkValue})"
        )
        assert framework_state == {
            "value": "after",
            "changes": 1,
            "frameworkValue": "after",
        }
    finally:
        if record is not None:
            await adapter.close_page(record)
        await adapter.close()
        server.close()
        await server.wait_closed()
