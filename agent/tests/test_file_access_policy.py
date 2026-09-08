from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from codito_protocol.device_read import normalize_read_scope

from codito_agent.approvals import ApprovalDecision, ApprovalManager
from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.file_targets import resolve_file_target
from codito_agent.models import ProjectMode
from codito_agent.patching import PatchService
from codito_agent.protocol_adapter import AgentProtocolAdapter
from codito_agent.read_tools import ProjectReadService


def setup(tmp_path, project_root, mode, decision):
    database = AgentDatabase(tmp_path / "policy.sqlite")
    project = database.register_project("Source", project_root, mode)
    prompts = []

    async def prompt(request):
        prompts.append(request)
        return decision

    approvals = ApprovalManager(prompt, database)
    adapter = AgentProtocolAdapter(
        database,
        ProjectReadService(database),
        PatchService(database, tmp_path / "journal"),
        object(),
        device_id="device_abcdefghijkl",
        account_id="account_abcdefghijkl",
        approvals=approvals,
    )
    return adapter, project, prompts, approvals


async def execute(adapter, tool, request):
    return await adapter.execute(
        tool,
        request,
        grant_id="grant_abcdefghijkl",
        link_id="link_abcdefghijklmnop",
        connection_epoch=1,
        deadline_at=datetime.now(UTC) + timedelta(seconds=5),
    )


@pytest.mark.parametrize(
    "mode,allowed",
    [
        (ProjectMode.NATIVE_APPROVAL, False),
        (ProjectMode.NATIVE_PROJECT, True),
        (ProjectMode.FULL_ACCESS, True),
        (ProjectMode.NATIVE_TRUSTED, False),
    ],
)
@pytest.mark.asyncio
async def test_local_read_and_delete_follow_same_policy(tmp_path, project_root, mode, allowed):
    raw = b"example\n"
    (project_root / "file.txt").write_bytes(raw)
    adapter, project, prompts, _ = setup(tmp_path, project_root, mode, ApprovalDecision.DENY)
    read = {"operation": "read_file", "project_id": project.project_id, "path": "file.txt"}
    delete = {
        "project_id": project.project_id,
        "patch": "*** Begin Patch\n*** Delete File: file.txt\n*** End Patch",
        "base_hashes": {"file.txt": hashlib.sha256(raw).hexdigest()},
        "idempotency_key": "delete_policy_123",
    }
    if allowed:
        assert (await execute(adapter, "project_read", read)).structured["sha256"]
        assert (await execute(adapter, "project_apply_patch", delete)).structured["applied"]
        assert not (project_root / "file.txt").exists()
        assert not prompts
    else:
        for tool, request in (("project_read", read), ("project_apply_patch", delete)):
            with pytest.raises(AgentError, match="denied"):
                await execute(adapter, tool, request)
        assert (project_root / "file.txt").read_bytes() == raw
        assert len(prompts) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows pinned scope handles")
@pytest.mark.asyncio
async def test_other_registered_project_still_requires_source_approval(tmp_path, project_root):
    adapter, source, prompts, _ = setup(
        tmp_path, project_root, ProjectMode.NATIVE_PROJECT, ApprovalDecision.DENY
    )
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "data.txt").write_text("private", encoding="utf-8")
    adapter.database.register_project("Other", outside, ProjectMode.FULL_ACCESS)
    with pytest.raises(AgentError, match="denied"):
        await execute(
            adapter,
            "project_read",
            {
                "operation": "read_file",
                "project_id": source.project_id,
                "scope_path": str(outside),
                "path": "data.txt",
                "purpose": "Read other project",
            },
        )
    assert len(prompts) == 1
    assert prompts[0].project_id == source.project_id
    assert prompts[0].requested_external_paths == (normalize_read_scope(str(outside)),)


@pytest.mark.skipif(os.name != "nt", reason="Windows pinned scope handles")
@pytest.mark.asyncio
async def test_external_patch_is_hash_bound_and_not_a_project_registration(tmp_path, project_root):
    adapter, source, prompts, _ = setup(
        tmp_path, project_root, ProjectMode.NATIVE_PROJECT, ApprovalDecision.ALLOW_ONCE
    )
    outside = tmp_path / "external"
    outside.mkdir()
    request = {
        "project_id": source.project_id,
        "scope_path": str(outside),
        "purpose": "Add file",
        "patch": "*** Begin Patch\n*** Add File: added.txt\n+hello\n*** End Patch",
        "base_hashes": {"added.txt": None},
        "idempotency_key": "external_patch_123",
    }
    assert (await execute(adapter, "project_apply_patch", request)).structured["applied"]
    assert (outside / "added.txt").read_text() == "hello\n"
    assert len(adapter.database.list_projects()) == 1
    assert len(prompts) == 1 and prompts[0].capability == "files:write"
    second = tmp_path / "another"
    second.mkdir()
    with pytest.raises(AgentError, match="different patch"):
        await execute(adapter, "project_apply_patch", {**request, "scope_path": str(second)})
    assert not (second / "added.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows pinned scope handles")
@pytest.mark.asyncio
async def test_saved_read_permission_never_authorizes_external_patch(tmp_path, project_root):
    adapter, source, prompts, _ = setup(
        tmp_path, project_root, ProjectMode.NATIVE_PROJECT, ApprovalDecision.ALLOW_ALWAYS_READ
    )
    outside = tmp_path / "external"
    outside.mkdir()
    read = {
        "operation": "list_directory",
        "project_id": source.project_id,
        "scope_path": str(outside),
        "purpose": "Read this folder",
    }
    await execute(adapter, "project_read", read)
    await execute(adapter, "project_read", read)
    assert len(prompts) == 1
    with pytest.raises(AgentError, match="Always allow is unavailable"):
        await execute(
            adapter,
            "project_apply_patch",
            {
                "project_id": source.project_id,
                "scope_path": str(outside),
                "purpose": "Write",
                "patch": "*** Begin Patch\n*** Add File: denied.txt\n+no\n*** End Patch",
                "base_hashes": {"denied.txt": None},
                "idempotency_key": "external_denied_123",
            },
        )
    assert not (outside / "denied.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows pinned scope handles")
def test_external_journal_recovers_original_target_not_origin_project(
    tmp_path, project_root, monkeypatch
):
    database = AgentDatabase(tmp_path / "recover.sqlite")
    project = database.register_project("Origin", project_root, ProjectMode.NATIVE_PROJECT)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project_root / "file.txt").write_bytes(b"origin must remain unchanged\n")
    (outside / "file.txt").write_bytes(b"old\n")
    service = PatchService(database, tmp_path / "journal")
    original_write_manifest = service._write_manifest

    class Crash(BaseException):
        pass

    def crash_after_mutation(transaction, manifest):
        if manifest["state"] == "committed":
            raise Crash()
        original_write_manifest(transaction, manifest)

    monkeypatch.setattr(service, "_write_manifest", crash_after_mutation)
    scope = normalize_read_scope(str(outside))
    with resolve_file_target(project, scope) as target, pytest.raises(Crash):
        assert target.pinned_scope is not None
        target.pinned_scope.release_for_mutation()
        service.apply(
            project_id=project.project_id,
            scope_path=scope,
            target_project=target.project,
            patch="*** Begin Patch\n*** Update File: file.txt\n@@\n-old\n+new\n*** End Patch",
            base_hashes={"file.txt": hashlib.sha256(b"old\n").hexdigest()},
            idempotency_key="external_recovery_123",
            account_id="account_abcdefghijkl",
            grant_id="grant_abcdefghijkl",
            link_id="link_abcdefghijklmnop",
            device_id="device_abcdefghijkl",
        )
    assert (outside / "file.txt").read_bytes() == b"new\n"
    manifests = list((tmp_path / "journal").glob("*/manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["scope_path"] == str(outside)
    recovered = PatchService(database, tmp_path / "journal").recover()
    assert len(recovered) == 1
    assert (outside / "file.txt").read_bytes() == b"old\n"
    assert (project_root / "file.txt").read_bytes() == b"origin must remain unchanged\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["preflight", "first_write"])
async def test_revocation_during_patch_never_leaves_committed_changes(
    tmp_path, project_root, monkeypatch, boundary
):
    adapter, source, _, approvals = setup(
        tmp_path, project_root, ProjectMode.NATIVE_PROJECT, ApprovalDecision.DENY
    )
    raw = b"old\n"
    (project_root / "one.txt").write_bytes(raw)
    (project_root / "two.txt").write_bytes(raw)
    if boundary == "preflight":
        original = adapter.patches._preflight

        def revoke_after_preflight(*args, **kwargs):
            result = original(*args, **kwargs)
            approvals.revoke_read_permissions()
            return result

        monkeypatch.setattr(adapter.patches, "_preflight", revoke_after_preflight)
    else:
        original_replace = adapter.patches._atomic_replace
        changed = False

        def revoke_after_write(target, data):
            nonlocal changed
            original_replace(target, data)
            if not changed:
                changed = True
                approvals.revoke_read_permissions()

        monkeypatch.setattr(adapter.patches, "_atomic_replace", revoke_after_write)
    with pytest.raises(AgentError, match="approval_expired"):
        await execute(
            adapter,
            "project_apply_patch",
            {
                "project_id": source.project_id,
                "patch": "*** Begin Patch\n*** Update File: one.txt\n@@\n-old\n+new\n"
                "*** Update File: two.txt\n@@\n-old\n+new\n*** End Patch",
                "base_hashes": {
                    name: hashlib.sha256(raw).hexdigest() for name in ("one.txt", "two.txt")
                },
                "idempotency_key": "revoked_patch_123",
            },
        )
    assert (project_root / "one.txt").read_bytes() == raw
    assert (project_root / "two.txt").read_bytes() == raw


@pytest.mark.asyncio
async def test_cancelled_patch_drains_worker_without_committing(
    tmp_path, project_root, monkeypatch
):
    adapter, source, _, _ = setup(
        tmp_path, project_root, ProjectMode.NATIVE_PROJECT, ApprovalDecision.DENY
    )
    raw = b"old\n"
    (project_root / "file.txt").write_bytes(raw)
    entered, resume = threading.Event(), threading.Event()
    original = adapter.patches._preflight

    def wait_preflight(*args, **kwargs):
        entered.set()
        assert resume.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter.patches, "_preflight", wait_preflight)
    task = asyncio.create_task(
        execute(
            adapter,
            "project_apply_patch",
            {
                "project_id": source.project_id,
                "patch": "*** Begin Patch\n*** Update File: file.txt\n"
                "@@\n-old\n+new\n*** End Patch",
                "base_hashes": {"file.txt": hashlib.sha256(raw).hexdigest()},
                "idempotency_key": "cancelled_patch_123",
            },
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (project_root / "file.txt").read_bytes() == raw
