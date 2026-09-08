from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from codito_protocol import DeviceReadInput

from codito_agent.approvals import ApprovalDecision, ApprovalManager, ApprovalRequest, ApprovalRisk
from codito_agent.db import AgentDatabase
from codito_agent.device_paths import WindowsReadScope
from codito_agent.device_read import DeviceReadService
from codito_agent.errors import AgentError


def read_request() -> ApprovalRequest:
    return ApprovalManager.build_request(
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        grant_id="grant_abcdefghijklmn",
        link_id="link_abcdefghijklmnop",
        project_id="",
        project_title="Outside projects",
        capability="device:read",
        action_digest="a" * 64,
        connection_epoch=1,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        risk=ApprovalRisk.READ,
        summary="Inspect test folder",
        requested_external_paths=("C:/example",),
    )


@pytest.mark.asyncio
async def test_saved_read_is_identity_bound_persistent_and_revocable(tmp_path: Path) -> None:
    prompts = []

    async def prompt(request):
        prompts.append(request)
        assert request.persistent_read_eligible
        return ApprovalDecision.ALLOW_ALWAYS_READ

    db = AgentDatabase(tmp_path / "db.sqlite3")
    manager = ApprovalManager(prompt, db)
    original = read_request()
    await manager.request(original, session_eligible=False, read_scope=("C:/example", "inode1"))
    # Reconnect/restart and a different file/action within the same scope reuse read consent.
    manager = ApprovalManager(prompt, AgentDatabase(db.path))
    await manager.request(
        replace(original, connection_epoch=9, action_digest="b" * 64),
        session_eligible=False,
        read_scope=("C:/example", "inode1"),
    )
    assert len(prompts) == 1
    for name in ("account_id", "device_id", "link_id", "grant_id"):
        await manager.request(
            replace(original, **{name: "different_abcdefghijkl"}),
            session_eligible=False,
            read_scope=("C:/example", "inode1"),
        )
    assert len(prompts) == 5
    await manager.request(original, session_eligible=False, read_scope=("C:/example", "inode2"))
    assert len(prompts) == 6
    assert manager.revoke_read_permissions() == 5
    assert db.list_read_permissions() == []
    await manager.request(original, session_eligible=False, read_scope=("C:/example", "inode2"))
    assert len(prompts) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [ApprovalDecision.DENY, ApprovalDecision.ALLOW_SESSION])
async def test_denial_never_enumerates_or_reads(tmp_path: Path, decision: ApprovalDecision) -> None:
    async def prompt(_):
        return decision

    scope = MagicMock(spec=WindowsReadScope)
    scope.__enter__.return_value = scope
    scope.identity = "inode"
    manager = ApprovalManager(prompt, AgentDatabase(tmp_path / "db.sqlite3"))
    service = DeviceReadService(
        manager, account_id="account", device_id="device", scope_factory=lambda _: scope
    )
    with pytest.raises(AgentError):
        await service.execute(
            DeviceReadInput(operation="list_directory", scope_path="C:/", purpose="Inspect"),
            grant_id="grant",
            link_id="link",
            connection_epoch=1,
            deadline_at=datetime.now(UTC) + timedelta(seconds=5),
        )
    scope.directory.assert_not_called()
    scope.read_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_always_cannot_authorize_shell_or_survive_pending_revocation(tmp_path: Path) -> None:
    manager = None

    async def prompt(_):
        manager.revoke_read_permissions()
        return ApprovalDecision.ALLOW_ALWAYS_READ

    db = AgentDatabase(tmp_path / "db.sqlite3")
    manager = ApprovalManager(prompt, db)
    with pytest.raises(AgentError, match="invalidated"):
        await manager.request(
            read_request(), session_eligible=False, read_scope=("C:/example", "inode")
        )
    assert db.list_read_permissions() == []
    with pytest.raises(AgentError, match="read-only"):
        await manager.request(
            replace(read_request(), capability="shell:execute"),
            session_eligible=False,
            read_scope=("C:/example", "inode"),
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows handles")
def test_windows_handles_block_rename_and_hardlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_bytes(b"test\n")
    with WindowsReadScope(str(root)) as scope:
        assert scope.read_bytes("a.txt", lambda: None) == b"test\n"
        with pytest.raises(OSError):
            root.rename(tmp_path / "moved")
        with pytest.raises(OSError):
            (root / "a.txt").rename(root / "moved.txt")
    os.link(root / "a.txt", root / "hard.txt")
    with WindowsReadScope(str(root)) as scope, pytest.raises(AgentError, match="Hardlinked"):
        scope.read_bytes("hard.txt", lambda: None)


@pytest.mark.skipif(os.name != "nt", reason="Windows handles")
@pytest.mark.asyncio
async def test_approved_windows_read_limits_and_no_grant_for_allow_once(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    prompts = []

    async def prompt(request):
        prompts.append(request)
        return ApprovalDecision.ALLOW_ONCE

    db = AgentDatabase(tmp_path / "db.sqlite3")
    service = DeviceReadService(
        ApprovalManager(prompt, db), account_id="account", device_id="device"
    )
    binding = dict(
        grant_id="grant",
        link_id="link",
        connection_epoch=1,
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    response = await service.execute(
        DeviceReadInput(
            operation="read_file", scope_path=str(root), path="a.txt", purpose="Test", max_lines=1
        ),
        **binding,
    )
    assert response.structured["numbered_text"] == "1: one\n"
    assert response.structured["newline"] == "crlf"
    assert response.structured["next_line"] == 2
    assert response.structured["truncated"]
    listing = await service.execute(
        DeviceReadInput(operation="list_directory", scope_path=str(root), purpose="Test"), **binding
    )
    assert listing.structured["entries"][0]["name"] == "a.txt"
    assert len(prompts) == 2
    assert db.list_read_permissions() == []


@pytest.mark.asyncio
async def test_pending_read_expires(tmp_path: Path) -> None:
    async def prompt(_):
        await asyncio.sleep(1)
        return ApprovalDecision.ALLOW_ALWAYS_READ

    db = AgentDatabase(tmp_path / "db.sqlite3")
    manager = ApprovalManager(prompt, db)
    with pytest.raises(AgentError, match="expired"):
        await manager.request(
            replace(read_request(), deadline_at=datetime.now(UTC) + timedelta(milliseconds=20)),
            session_eligible=False,
            read_scope=("C:/example", "inode"),
        )
    assert db.list_read_permissions() == []
