from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codito_agent.db import AgentDatabase
from codito_agent.errors import AgentError
from codito_agent.models import OperationState
from codito_agent.patching import AnchoredPatchParser, PatchService, _request_digest


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


BINDING = {
    "account_id": "account_abcdefghijkl",
    "grant_id": "grant_abcdefghijklmn",
    "link_id": "link_abcdefghijklmnop",
    "device_id": "device_abcdefghijkl",
}


def test_patch_update_move_add_delete_and_idempotency(tmp_path: Path, project_root: Path) -> None:
    original = b"alpha\nbeta\ngamma\n"
    delete_value = b"remove me\n"
    (project_root / "old.txt").write_bytes(original)
    (project_root / "delete.txt").write_bytes(delete_value)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = PatchService(database, tmp_path / "journals")
    patch = """*** Begin Patch
*** Update File: old.txt
*** Move to: moved.txt
@@
 alpha
-beta
+BETA
 gamma
*** Add File: added.txt
+new
*** Delete File: delete.txt
*** End Patch"""
    result = service.apply(
        project_id=project.project_id,
        patch=patch,
        base_hashes={
            "old.txt": sha(original),
            "moved.txt": None,
            "added.txt": None,
            "delete.txt": sha(delete_value),
        },
        idempotency_key="patch_key_abcdefghijkl",
        **BINDING,
    )
    assert not (project_root / "old.txt").exists()
    assert (project_root / "moved.txt").read_text() == "alpha\nBETA\ngamma\n"
    assert (project_root / "added.txt").read_text() == "new\n"
    assert not (project_root / "delete.txt").exists()
    assert [item["operation"] for item in result.structured["files"]] == [
        "move",
        "add",
        "delete",
    ]
    repeated = service.apply(
        project_id=project.project_id,
        patch=patch,
        base_hashes={
            "old.txt": sha(original),
            "moved.txt": None,
            "added.txt": None,
            "delete.txt": sha(delete_value),
        },
        idempotency_key="patch_key_abcdefghijkl",
        **BINDING,
    )
    assert repeated.structured == result.structured


def test_dry_run_does_not_mutate(tmp_path: Path, project_root: Path) -> None:
    value = b"before\n"
    target = project_root / "file.txt"
    target.write_bytes(value)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = PatchService(database, tmp_path / "journals")
    result = service.apply(
        project_id=project.project_id,
        patch="*** Begin Patch\n*** Update File: file.txt\n@@\n-before\n+after\n*** End Patch",
        base_hashes={"file.txt": sha(value)},
        idempotency_key="dry_run_abcdefghijkl",
        dry_run=True,
        **BINDING,
    )
    assert not result.structured["applied"]
    assert target.read_bytes() == value


def test_ambiguous_context_and_base_hash_fail_closed(tmp_path: Path, project_root: Path) -> None:
    value = b"same\nother\nsame\n"
    (project_root / "file.txt").write_bytes(value)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = PatchService(database, tmp_path / "journals")
    with pytest.raises(AgentError) as error:
        service.apply(
            project_id=project.project_id,
            patch="*** Begin Patch\n*** Update File: file.txt\n@@\n-same\n+changed\n*** End Patch",
            base_hashes={"file.txt": sha(value)},
            idempotency_key="ambiguous_abcdefgh",
            dry_run=True,
            **BINDING,
        )
    assert error.value.code == "patch_conflict"
    assert (project_root / "file.txt").read_bytes() == value


def test_parser_rejects_unanchored_lines() -> None:
    with pytest.raises(AgentError):
        AnchoredPatchParser().parse(
            "*** Begin Patch\n*** Update File: file.txt\n@@\nunmarked\n*** End Patch"
        )


def test_patch_idempotency_is_bound_to_oauth_grant(tmp_path: Path, project_root: Path) -> None:
    value = b"before\n"
    (project_root / "file.txt").write_bytes(value)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = PatchService(database, tmp_path / "journals")
    arguments = {
        "project_id": project.project_id,
        "patch": "*** Begin Patch\n*** Update File: file.txt\n@@\n-before\n+after\n*** End Patch",
        "base_hashes": {"file.txt": sha(value)},
        "idempotency_key": "bound_patch_abcdefgh",
        **BINDING,
    }
    service.apply(**arguments)
    with pytest.raises(AgentError) as error:
        service.apply(**{**arguments, "grant_id": "grant_different0000"})
    assert error.value.code == "idempotency_conflict"


def test_recovery_finalizes_commit_before_database_terminal_boundary(
    tmp_path: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = b"before\n"
    target = project_root / "file.txt"
    target.write_bytes(value)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = PatchService(database, tmp_path / "journals")
    original_put = database.put_idempotency
    calls = 0

    def fail_terminal(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected crash after committed manifest")
        original_put(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(database, "put_idempotency", fail_terminal)
    with pytest.raises(RuntimeError, match="injected crash"):
        service.apply(
            project_id=project.project_id,
            patch="*** Begin Patch\n*** Update File: file.txt\n@@\n-before\n+after\n*** End Patch",
            base_hashes={"file.txt": sha(value)},
            idempotency_key="crash_patch_abcdefgh",
            **BINDING,
        )
    assert target.read_bytes() == b"after\n"
    assert any((tmp_path / "journals").iterdir())

    monkeypatch.setattr(database, "put_idempotency", original_put)
    reconciled = service.reconcile_duplicate(
        project_id=project.project_id,
        patch="*** Begin Patch\n*** Update File: file.txt\n@@\n-before\n+after\n*** End Patch",
        base_hashes={"file.txt": sha(value)},
        idempotency_key="crash_patch_abcdefgh",
        dry_run=False,
        **BINDING,
    )
    assert reconciled is not None and reconciled.structured["applied"] is True
    record = database.get_idempotency(
        project.project_id,
        "project_apply_patch",
        "crash_patch_abcdefgh",
        **BINDING,
    )
    assert record is not None and record["state"] == "succeeded"
    assert not any((tmp_path / "journals").iterdir())


def test_duplicate_patch_retries_only_pre_manifest_running_marker(
    tmp_path: Path, project_root: Path
) -> None:
    value = b"before\n"
    target = project_root / "file.txt"
    target.write_bytes(value)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = PatchService(database, tmp_path / "journals")
    patch = "*** Begin Patch\n*** Update File: file.txt\n@@\n-before\n+after\n*** End Patch"
    request_digest = _request_digest(patch, {"file.txt": sha(value)}, False)
    database.put_idempotency(
        project.project_id,
        "project_apply_patch",
        "pre_manifest_crash_key",
        request_digest,
        OperationState.RUNNING,
        **BINDING,
    )

    assert (
        service.reconcile_duplicate(
            project_id=project.project_id,
            patch=patch,
            base_hashes={"file.txt": sha(value)},
            idempotency_key="pre_manifest_crash_key",
            dry_run=False,
            **BINDING,
        )
        is None
    )
    result = service.apply(
        project_id=project.project_id,
        patch=patch,
        base_hashes={"file.txt": sha(value)},
        idempotency_key="pre_manifest_crash_key",
        **BINDING,
    )
    assert result.structured["applied"] is True
    assert target.read_bytes() == b"after\n"


def test_commit_rejects_in_place_edit_after_preflight(
    tmp_path: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = b"before\n"
    raced = b"changed outside Codito\n"
    target = project_root / "file.txt"
    target.write_bytes(original)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    service = PatchService(database, tmp_path / "journals")
    original_commit = service._commit

    def race_before_commit(*args: object, **kwargs: object) -> None:
        target.write_bytes(raced)
        original_commit(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_commit", race_before_commit)
    with pytest.raises(AgentError) as error:
        service.apply(
            project_id=project.project_id,
            patch="*** Begin Patch\n*** Update File: file.txt\n@@\n-before\n+after\n*** End Patch",
            base_hashes={"file.txt": sha(original)},
            idempotency_key="raced_patch_abcdefgh",
            **BINDING,
        )

    assert error.value.code == "patch_conflict"
    assert target.read_bytes() == raced
    assert not any((tmp_path / "journals").iterdir())


def test_patch_rejects_disabled_project(tmp_path: Path, project_root: Path) -> None:
    value = b"before\n"
    (project_root / "file.txt").write_bytes(value)
    database = AgentDatabase(tmp_path / "agent.sqlite3")
    project = database.register_project("Example", project_root)
    database.disable_project(project.project_id)
    service = PatchService(database, tmp_path / "journals")
    with pytest.raises(AgentError) as error:
        service.apply(
            project_id=project.project_id,
            patch="*** Begin Patch\n*** Update File: file.txt\n@@\n-before\n+after\n*** End Patch",
            base_hashes={"file.txt": sha(value)},
            idempotency_key="disabled_patch_key",
            dry_run=True,
            **BINDING,
        )
    assert error.value.code == "project_disabled"
