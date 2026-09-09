"""Independent regressions for the real transport and freshness promises."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest
from lsprotocol import types as lsp

from codito_agent.code_intelligence import lsp_client
from codito_agent.code_intelligence.lsp_client import (
    LspClientError,
    LspServerProfile,
    ManagedLspClient,
)
from codito_agent.code_intelligence.lsp_provider import (
    LspProvider,
    unicode_scalar_to_utf16_offset,
    utf16_offset_to_unicode_scalar,
)
from codito_agent.code_intelligence.lsp_router import LspRouter
from codito_agent.code_intelligence.snapshot import capture_snapshot, scan_workspace
from codito_agent.code_intelligence.source import source_snapshot
from codito_agent.code_intelligence.types import CapabilitySet
from codito_agent.errors import AgentError

FIXTURE_SERVER = Path(__file__).with_name("_fake_code_intelligence_lsp.py")


def profile(*args: str) -> LspServerProfile:
    return LspServerProfile([sys.executable, str(FIXTURE_SERVER), *args], name="fixture")


def test_typed_language_server_capabilities_are_recognized() -> None:
    caps = CapabilitySet.from_lsp_capabilities(
        lsp.ServerCapabilities(
            definition_provider=True,
            references_provider=True,
            hover_provider=True,
            document_symbol_provider=True,
            workspace_symbol_provider=True,
        )
    )
    assert caps.definition and caps.references and caps.hover
    assert caps.document_symbols and caps.workspace_symbols


@pytest.mark.asyncio
async def test_real_stdio_server_starts_queries_and_exits(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def example():\n    return 1\n", encoding="utf-8")
    client = ManagedLspClient(profile(), tmp_path)
    process = None
    try:
        await asyncio.wait_for(client.start(), 8)
        assert client.is_running
        assert client.capabilities.definition
        assert client._client is not None
        process = client._client._server
        await client.notify_did_open(source.as_uri(), "python", source.read_text(), 1)
        result = await client.request_workspace_symbols("example")
        assert result and result[0].name == "example"
        hover = await client.request_hover(source.as_uri(), 0, 5)
        assert (
            hover.contents.value
            == "sha256="
            + hashlib.sha256(source.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        )
    finally:
        await asyncio.wait_for(client.shutdown(), 8)
    assert not client.is_running
    assert process is not None and process.returncode is not None


@pytest.mark.asyncio
async def test_timeout_is_failure_not_empty_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "module.py"
    source.write_text("def example():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(lsp_client, "_REQUEST_TIMEOUT_S", 0.15)
    client = ManagedLspClient(profile("--hang-hover"), tmp_path)
    try:
        await asyncio.wait_for(client.start(), 8)
        with pytest.raises(LspClientError, match="timed out"):
            await client.request_hover(source.as_uri(), 0, 5)
        assert not client.is_running
    finally:
        await asyncio.wait_for(client.shutdown(), 8)


@pytest.mark.asyncio
async def test_document_change_updates_versioned_server_state(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def before():\n    return 1\n", encoding="utf-8")
    client = ManagedLspClient(profile(), tmp_path)
    try:
        await asyncio.wait_for(client.start(), 8)
        version = await client.ensure_document(source.as_uri(), "python", source.read_text())
        assert version == 1
        changed = "def after():\n    return 2\n"
        version = await client.ensure_document(source.as_uri(), "python", changed)
        assert version == 2
        hover = await client.request_hover(source.as_uri(), 0, 5)
        assert hover is not None
        assert hover.contents.value == "sha256=" + hashlib.sha256(changed.encode()).hexdigest()
    finally:
        await asyncio.wait_for(client.shutdown(), 8)


@pytest.mark.asyncio
async def test_pull_diagnostics_reuse_result_id_without_false_empty(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def example():\n    return 1\n", encoding="utf-8")
    client = ManagedLspClient(profile(), tmp_path)
    try:
        await asyncio.wait_for(client.start(), 8)
        await client.ensure_document(source.as_uri(), "python", source.read_text())
        first, first_state = await client.diagnostic_snapshot(source.as_uri())
        second, second_state = await client.diagnostic_snapshot(source.as_uri())
        assert first == second == []
        assert first_state == second_state == "ready"
    finally:
        await asyncio.wait_for(client.shutdown(), 8)


@pytest.mark.asyncio
async def test_push_diagnostics_are_bound_to_document_version(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("BAD example\n", encoding="utf-8")
    client = ManagedLspClient(profile("--push-diagnostics"), tmp_path)
    try:
        await asyncio.wait_for(client.start(), 8)
        await client.ensure_document(source.as_uri(), "python", source.read_text())
        diagnostics, state = await client.diagnostic_snapshot(source.as_uri())
        assert state == "ready"
        assert len(diagnostics) == 1 and diagnostics[0].code == "fixture-bad"
        changed = "def good():\n    return 1\n"
        await client.ensure_document(source.as_uri(), "python", changed)
        diagnostics, state = await client.diagnostic_snapshot(source.as_uri())
        assert state == "ready"
        assert diagnostics == []
    finally:
        await asyncio.wait_for(client.shutdown(), 8)


@pytest.mark.skipif(os.name != "nt", reason="Windows file-URI case normalization regression")
@pytest.mark.asyncio
async def test_push_diagnostics_accept_provider_normalized_windows_uri(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("BAD example\n", encoding="utf-8")
    client = ManagedLspClient(
        profile("--push-diagnostics", "--alternate-uri-diagnostics"), tmp_path
    )
    try:
        await asyncio.wait_for(client.start(), 8)
        await client.ensure_document(source.as_uri(), "python", source.read_text())
        diagnostics, state = await client.diagnostic_snapshot(source.as_uri())
        assert state == "ready"
        assert len(diagnostics) == 1 and diagnostics[0].code == "fixture-bad"
    finally:
        await asyncio.wait_for(client.shutdown(), 8)


@pytest.mark.asyncio
async def test_versionless_push_diagnostics_are_partial_not_clean(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def good():\n    return 1\n", encoding="utf-8")
    client = ManagedLspClient(profile("--push-diagnostics", "--versionless-diagnostics"), tmp_path)
    try:
        await asyncio.wait_for(client.start(), 8)
        await client.ensure_document(source.as_uri(), "python", source.read_text())
        diagnostics, state = await client.diagnostic_snapshot(source.as_uri())
        assert diagnostics == []
        assert state == "partial"
    finally:
        await asyncio.wait_for(client.shutdown(), 8)


def test_utf16_conversion_rejects_invalid_columns_and_split_surrogates() -> None:
    line = "a\U0001f600b"
    assert unicode_scalar_to_utf16_offset(line, 3) == 3
    assert utf16_offset_to_unicode_scalar(line, 3) == 3
    with pytest.raises(AgentError, match="outside"):
        unicode_scalar_to_utf16_offset(line, 6)
    with pytest.raises(AgentError, match="splits"):
        utf16_offset_to_unicode_scalar(line, 2)


@pytest.mark.asyncio
async def test_lsp_hierarchies_honor_requested_depth(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("root\ncaller1\ncaller2\ncallee1\ncallee2\n", encoding="utf-8")
    snapshot = await capture_snapshot(tmp_path)
    client = ManagedLspClient(profile(), tmp_path)
    try:
        await asyncio.wait_for(client.start(), 8)
        await client.ensure_document(source.as_uri(), "python", source.read_text())
        provider = LspProvider(client, tmp_path)
        with source_snapshot(snapshot):
            depth_one = await provider.call_hierarchy(
                "module.py", 1, 1, "incoming", 1, 10, snapshot.generation
            )
            depth_two = await provider.call_hierarchy(
                "module.py", 1, 1, "incoming", 2, 10, snapshot.generation
            )
            types = await provider.type_hierarchy(
                "module.py", 1, 1, "subtypes", 2, 10, snapshot.generation
            )
        assert [item.depth for item in depth_one.incoming_calls] == [1]
        assert [item.depth for item in depth_two.incoming_calls] == [1, 2]
        assert [item.depth for item in types.subtypes] == [1, 2]
    finally:
        await asyncio.wait_for(client.shutdown(), 8)


@pytest.mark.asyncio
async def test_every_watched_file_change_is_delivered(tmp_path: Path) -> None:
    count = 520
    for index in range(count):
        (tmp_path / f"module_{index:04}.py").write_text(f"def before_{index}(): pass\n")
    router = LspRouter(tmp_path, manual_profile=profile())
    try:
        session = await asyncio.wait_for(router._provider("module_0000.py"), 8)
        assert session is not None
        client, _ = session
        changed = []
        for index in range(count):
            file = tmp_path / f"module_{index:04}.py"
            file.write_text(f"def after_{index}(): pass\n")
            changed.append(file.name)
        await router.notify_changes([], changed, [])
        results = await client.request_workspace_symbols("after_")
        assert results is not None and len(results) == count
    finally:
        await asyncio.wait_for(router.shutdown(), 8)


def test_snapshot_honors_gitignore_and_never_reads_env(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n*.generated.py\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "hidden.py").write_text("def hidden(): pass\n")
    (tmp_path / "visible.py").write_text("def visible(): pass\n")
    (tmp_path / "other.generated.py").write_text("def generated(): pass\n")
    (tmp_path / ".env").write_text("FAKE_TEST_SECRET=never_index\n")
    (tmp_path / ".run").mkdir()
    (tmp_path / ".run" / "fixture.py").write_text("def local_task(): pass\n")
    result = scan_workspace(tmp_path)
    assert "visible.py" in result
    assert ".gitignore" in result
    assert ".env" not in result
    assert "ignored/hidden.py" not in result
    assert "other.generated.py" not in result
    assert ".run/fixture.py" not in result


def test_same_size_same_mtime_edits_change_snapshot_hash(tmp_path: Path) -> None:
    file = tmp_path / "module.py"
    file.write_text("def first(): pass\n")
    timestamp = file.stat()
    before = scan_workspace(tmp_path)
    file.write_text("def other(): pass\n")
    os.utime(file, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
    after = scan_workspace(tmp_path)
    assert before[file.name].content_hash != after[file.name].content_hash
