from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from .db import AgentDatabase
from .errors import AgentError
from .file_targets import resolve_file_target
from .models import OperationState, Project
from .operation_gate import ProjectOperationGate
from .paths import ProjectPathResolver, ValidatedPath, validate_relative_path
from .read_tools import ToolResponse

MAX_PATCH_BYTES = 2_097_152
MAX_PATCH_FILE_BYTES = 8_388_608
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SECTION_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")


@dataclass(frozen=True, slots=True)
class Hunk:
    lines: tuple[tuple[Literal[" ", "+", "-"], str], ...]


@dataclass(frozen=True, slots=True)
class PatchSection:
    action: Literal["add", "update", "delete", "move"]
    path: str
    destination: str | None = None
    add_lines: tuple[str, ...] = ()
    hunks: tuple[Hunk, ...] = ()


@dataclass(slots=True)
class _TextFile:
    lines: list[str]
    newline: str
    final_newline: bool
    bom: bool = False

    @classmethod
    def decode(cls, raw: bytes) -> _TextFile:
        bom = raw.startswith(b"\xef\xbb\xbf")
        body = raw[3:] if bom else raw
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentError("unsupported_encoding", "Patches require UTF-8 project files") from exc
        if "\r\n" in text:
            newline = "\r\n"
        elif "\n" in text:
            newline = "\n"
        elif "\r" in text:
            newline = "\r"
        else:
            newline = os.linesep
        final = text.endswith(("\r\n", "\n", "\r"))
        return cls(text.splitlines(), newline, final, bom)

    def encode(self) -> bytes:
        text = self.newline.join(self.lines)
        if self.final_newline and self.lines:
            text += self.newline
        raw = text.encode("utf-8")
        return b"\xef\xbb\xbf" + raw if self.bom else raw


@dataclass(frozen=True, slots=True)
class _Change:
    action: str
    source: str
    destination: str | None
    old_hash: str | None
    new_hash: str | None


@dataclass(slots=True)
class _Preflight:
    desired: dict[str, bytes | None] = field(default_factory=dict)
    validated: dict[str, ValidatedPath] = field(default_factory=dict)
    changes: list[_Change] = field(default_factory=list)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _no_check() -> None:
    """Direct local callers have no asynchronous consent lifecycle."""


def _request_digest(
    patch: str, base_hashes: dict[str, str | None], dry_run: bool, scope_path: str | None = None
) -> str:
    value: dict[str, Any] = {"patch": patch, "base_hashes": base_hashes, "dry_run": dry_run}
    if scope_path is not None:
        value["scope_path"] = scope_path
    encoded = json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return _sha(encoded)


class AnchoredPatchParser:
    """Parse Codito's intentionally small, exact, non-fuzzy patch grammar."""

    def parse(self, document: str) -> tuple[PatchSection, ...]:
        if not isinstance(document, str) or len(document.encode("utf-8")) > MAX_PATCH_BYTES:
            raise AgentError("invalid_patch", "Patch document is missing or too large")
        lines = document.splitlines()
        if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise AgentError(
                "invalid_patch", "Patch must have exact Begin Patch and End Patch markers"
            )
        sections: list[PatchSection] = []
        index = 1
        while index < len(lines) - 1:
            match = _SECTION_RE.fullmatch(lines[index])
            if match is None:
                raise AgentError("invalid_patch", "Expected an Add, Update, or Delete file section")
            action = match.group(1).lower()
            path = validate_relative_path(match.group(2))
            index += 1
            destination: str | None = None
            if (
                action == "update"
                and index < len(lines) - 1
                and lines[index].startswith("*** Move to: ")
            ):
                destination = validate_relative_path(lines[index][13:])
                action = "move"
                index += 1

            content_start = index
            while index < len(lines) - 1 and _SECTION_RE.fullmatch(lines[index]) is None:
                index += 1
            content = lines[content_start:index]
            if action == "add":
                if not content or any(not line.startswith("+") for line in content):
                    raise AgentError("invalid_patch", "Add sections contain only '+' lines")
                sections.append(
                    PatchSection("add", path, add_lines=tuple(line[1:] for line in content))
                )
                continue
            if action == "delete":
                if content:
                    raise AgentError("invalid_patch", "Delete sections cannot contain hunks")
                sections.append(PatchSection("delete", path))
                continue

            hunks: list[Hunk] = []
            cursor = 0
            while cursor < len(content):
                if content[cursor] != "@@":
                    raise AgentError(
                        "invalid_patch", "Each update or move hunk must start with '@@'"
                    )
                cursor += 1
                hunk_lines: list[tuple[Literal[" ", "+", "-"], str]] = []
                while cursor < len(content) and content[cursor] != "@@":
                    line = content[cursor]
                    if not line or line[0] not in {" ", "+", "-"}:
                        raise AgentError(
                            "invalid_patch", "Every hunk line needs a space, '+', or '-' prefix"
                        )
                    prefix = cast(Literal[" ", "+", "-"], line[0])
                    hunk_lines.append((prefix, line[1:]))
                    cursor += 1
                old_lines = [text for prefix, text in hunk_lines if prefix != "+"]
                if not old_lines:
                    raise AgentError("invalid_patch", "Insertion hunks need exact context")
                if not any(prefix in {"+", "-"} for prefix, _ in hunk_lines):
                    raise AgentError("invalid_patch", "A hunk must make a change")
                hunks.append(Hunk(tuple(hunk_lines)))
            if not hunks:
                raise AgentError(
                    "invalid_patch", "Update and move sections require at least one hunk"
                )
            sections.append(
                PatchSection(action, path, destination=destination, hunks=tuple(hunks))  # type: ignore[arg-type]
            )
        if not sections:
            raise AgentError("invalid_patch", "Patch must contain at least one file section")
        return tuple(sections)


def _apply_hunks(document: _TextFile, hunks: tuple[Hunk, ...], path: str) -> _TextFile:
    lines = list(document.lines)
    minimum_index = 0
    for hunk_number, hunk in enumerate(hunks, 1):
        expected = [text for prefix, text in hunk.lines if prefix != "+"]
        replacement = [text for prefix, text in hunk.lines if prefix != "-"]
        candidates = [
            index
            for index in range(minimum_index, len(lines) - len(expected) + 1)
            if lines[index : index + len(expected)] == expected
        ]
        if not candidates:
            raise AgentError(
                "patch_conflict",
                "Patch context does not match the current file",
                {"conflicts": [{"path": path, "hunk": hunk_number, "reason": "not_found"}]},
            )
        if len(candidates) != 1:
            raise AgentError(
                "patch_conflict",
                "Patch context is ambiguous",
                {"conflicts": [{"path": path, "hunk": hunk_number, "reason": "ambiguous"}]},
            )
        index = candidates[0]
        lines[index : index + len(expected)] = replacement
        minimum_index = index + len(replacement)
    return _TextFile(lines, document.newline, document.final_newline, document.bom)


class PatchService:
    def __init__(
        self,
        database: AgentDatabase,
        journal_root: Path,
        resolver: ProjectPathResolver | None = None,
        operation_gate: ProjectOperationGate | None = None,
    ) -> None:
        self.database = database
        self.journal_root = journal_root
        self.journal_root.mkdir(parents=True, exist_ok=True)
        self.resolver = resolver or ProjectPathResolver()
        self.parser = AnchoredPatchParser()
        self.operation_gate = operation_gate or ProjectOperationGate()
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _project_lock(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.Lock())

    @staticmethod
    def _normalize_base_hashes(base_hashes: dict[str, str | None]) -> dict[str, str | None]:
        if not isinstance(base_hashes, dict):
            raise AgentError("invalid_request", "base_hashes must be an object")
        normalized_hashes: dict[str, str | None] = {}
        for path, value in base_hashes.items():
            normalized = validate_relative_path(path)
            if value is not None and (not isinstance(value, str) or not _HASH_RE.fullmatch(value)):
                raise AgentError("invalid_request", "Every base hash must be SHA-256 or null")
            normalized_hashes[normalized] = value.lower() if value else None
        return normalized_hashes

    def reconcile_duplicate(
        self,
        *,
        project_id: str,
        patch: str,
        base_hashes: dict[str, str | None],
        idempotency_key: str,
        dry_run: bool,
        account_id: str,
        grant_id: str,
        link_id: str,
        device_id: str,
        scope_path: str | None = None,
        purpose: str = "",
    ) -> ToolResponse | None:
        """Return/recover a prior patch, or prove that a fresh retry is needed.

        This is called before prompting for destructive patch redelivery, so an
        already committed operation is reported without asking the user to
        authorize the same mutation again.
        """

        normalized_hashes = self._normalize_base_hashes(base_hashes)
        digest = _request_digest(patch, normalized_hashes, dry_run, scope_path)
        binding = {
            "account_id": account_id,
            "grant_id": grant_id,
            "link_id": link_id,
            "device_id": device_id,
        }
        project = self.database.get_project(project_id)
        if not project.enabled:
            raise AgentError("project_disabled", "The requested project is disabled")
        prior = self.database.get_idempotency(
            project_id, "project_apply_patch", idempotency_key, **binding
        )
        if prior is None:
            return None
        if prior["request_digest"] != digest:
            raise AgentError(
                "idempotency_conflict", "Idempotency key was reused for a different patch"
            )
        if prior["state"] == OperationState.SUCCEEDED.value and prior["result"] is not None:
            return ToolResponse(prior["result"], "Patch was already applied; returning its result.")
        self._reconcile_incomplete_attempt(
            project_id=project_id,
            idempotency_key=idempotency_key,
            request_digest=digest,
            binding=binding,
        )
        prior = self.database.get_idempotency(
            project_id, "project_apply_patch", idempotency_key, **binding
        )
        if prior is None:
            return None
        if prior["state"] == OperationState.SUCCEEDED.value and prior["result"] is not None:
            return ToolResponse(prior["result"], "Recovered the committed patch result.")
        raise AgentError(
            "outcome_unknown",
            "A prior patch attempt could not be reconciled deterministically",
            retryable=False,
        )

    def apply(
        self,
        *,
        project_id: str,
        patch: str,
        base_hashes: dict[str, str | None],
        idempotency_key: str,
        dry_run: bool = False,
        account_id: str,
        grant_id: str,
        link_id: str,
        device_id: str,
        reconcile_incomplete: bool = False,
        scope_path: str | None = None,
        purpose: str = "",
        target_project: Project | None = None,
        check: Callable[[], None] = _no_check,
    ) -> ToolResponse:
        check()
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise AgentError("invalid_request", "idempotency_key must contain 8 to 200 characters")
        normalized_hashes = self._normalize_base_hashes(base_hashes)
        digest = _request_digest(patch, normalized_hashes, dry_run, scope_path)
        binding = {
            "account_id": account_id,
            "grant_id": grant_id,
            "link_id": link_id,
            "device_id": device_id,
        }
        project = self.database.get_project(project_id)
        if not project.enabled:
            raise AgentError("project_disabled", "The requested project is disabled")
        if scope_path is not None:
            if target_project is None or target_project.root != Path(scope_path):
                raise AgentError(
                    "invalid_request", "External patch target was not locally resolved"
                )
            project = target_project
        prior = self.database.get_idempotency(
            project_id, "project_apply_patch", idempotency_key, **binding
        )
        if prior is not None:
            if prior["request_digest"] != digest:
                raise AgentError(
                    "idempotency_conflict", "Idempotency key was reused for a different patch"
                )
            if prior["state"] == OperationState.SUCCEEDED.value and prior["result"] is not None:
                return ToolResponse(
                    prior["result"], "Patch was already applied; returning its result."
                )
            if reconcile_incomplete:
                self._reconcile_incomplete_attempt(
                    project_id=project_id,
                    idempotency_key=idempotency_key,
                    request_digest=digest,
                    binding=binding,
                )
                prior = self.database.get_idempotency(
                    project_id, "project_apply_patch", idempotency_key, **binding
                )
                if (
                    prior is not None
                    and prior["state"] == OperationState.SUCCEEDED.value
                    and prior["result"] is not None
                ):
                    return ToolResponse(prior["result"], "Recovered the committed patch result.")
            if prior is not None:
                raise AgentError(
                    "outcome_unknown",
                    "A prior patch attempt did not record a terminal result",
                    retryable=False,
                )

        sections = self.parser.parse(patch)
        owner = f"patch:{idempotency_key}"
        with self.operation_gate.hold(project_id, owner), self._project_lock(project_id):
            check()
            preflight = self._preflight(project, sections, normalized_hashes, check=check)
            check()
            result = {
                "project_id": project_id,
                "idempotency_key": idempotency_key,
                "dry_run": dry_run,
                "applied": not dry_run,
                "files": [
                    {
                        "operation": change.action,
                        "path": change.source,
                        "destination": change.destination,
                        "old_sha256": change.old_hash,
                        "new_sha256": change.new_hash,
                    }
                    for change in preflight.changes
                ],
                "conflicts": [],
            }
            if dry_run:
                return ToolResponse(
                    result, f"Patch preflight passed for {len(preflight.changes)} file(s)."
                )
            journal_id = str(uuid.uuid4())
            result["journal_id"] = journal_id
            self.database.put_idempotency(
                project_id,
                "project_apply_patch",
                idempotency_key,
                digest,
                OperationState.RUNNING,
                account_id=account_id,
                grant_id=grant_id,
                link_id=link_id,
                device_id=device_id,
            )
            try:
                self._commit(
                    project,
                    preflight,
                    journal_id=journal_id,
                    idempotency_key=idempotency_key,
                    request_digest=digest,
                    result=result,
                    binding=binding,
                    check=check,
                )
            except Exception as exc:
                if not isinstance(exc, AgentError) or exc.code != "recovery_failed":
                    self.database.delete_idempotency(
                        project_id, "project_apply_patch", idempotency_key
                    )
                raise
            self.database.put_idempotency(
                project_id,
                "project_apply_patch",
                idempotency_key,
                digest,
                OperationState.SUCCEEDED,
                result,
                account_id=account_id,
                grant_id=grant_id,
                link_id=link_id,
                device_id=device_id,
            )
            shutil.rmtree(self.journal_root / journal_id, ignore_errors=True)
            return ToolResponse(
                result, f"Applied an atomic patch to {len(preflight.changes)} file(s)."
            )

    def _reconcile_incomplete_attempt(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        request_digest: str,
        binding: dict[str, str],
    ) -> None:
        """Recover one crash-interrupted patch before a relay redelivery.

        The daemon performs global recovery before connecting. This targeted
        path covers the narrow receipt/idempotency boundary without touching an
        unrelated live project's journal. If no manifest was ever made, no file
        mutation could have started, so removing only that stale RUNNING marker
        is safe; the normal base-hash preflight still guards the retry.
        """

        owner = f"patch:{idempotency_key}"
        matched = False
        with self.operation_gate.hold(project_id, owner), self._project_lock(project_id):
            for transaction in self.journal_root.iterdir():
                manifest_path = transaction / "manifest.json"
                if not transaction.is_dir() or not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise AgentError(
                        "recovery_failed",
                        "A patch recovery manifest could not be read safely",
                        {"journal_id": transaction.name},
                    ) from exc
                if (
                    str(manifest.get("project_id")) != project_id
                    or str(manifest.get("idempotency_key")) != idempotency_key
                ):
                    continue
                matched = True
                if (
                    str(manifest.get("request_digest")) != request_digest
                    or manifest.get("binding") != binding
                ):
                    raise AgentError(
                        "recovery_failed",
                        "Patch recovery binding does not match the redelivered operation",
                        {"journal_id": transaction.name},
                    )
                self._recover_transaction(transaction, manifest)

            prior = self.database.get_idempotency(
                project_id, "project_apply_patch", idempotency_key, **binding
            )
            if prior is not None and prior["state"] != OperationState.SUCCEEDED.value:
                if matched:
                    raise AgentError(
                        "recovery_failed",
                        "Patch recovery did not reach a deterministic terminal state",
                    )
                # `_commit` creates and fsyncs the manifest before its first
                # mutation. A RUNNING marker with no manifest is pre-mutation.
                self.database.delete_idempotency(project_id, "project_apply_patch", idempotency_key)

    def _preflight(
        self,
        project: Any,
        sections: tuple[PatchSection, ...],
        base_hashes: dict[str, str | None],
        *,
        check: Callable[[], None] = _no_check,
    ) -> _Preflight:
        required = {section.path for section in sections}
        required.update(
            section.destination for section in sections if section.destination is not None
        )
        if required != set(base_hashes):
            missing = sorted(required - set(base_hashes))
            unexpected = sorted(set(base_hashes) - required)
            raise AgentError(
                "invalid_request",
                "base_hashes must exactly cover every source path",
                {"missing": missing, "unexpected": unexpected},
            )
        touched: set[str] = set()
        preflight = _Preflight()
        for section in sections:
            check()
            paths = {section.path}
            if section.destination is not None:
                paths.add(section.destination)
            overlap = touched & paths
            if overlap:
                raise AgentError(
                    "invalid_patch",
                    "A path may appear in only one patch section",
                    {"paths": sorted(overlap)},
                )
            touched.update(paths)
            expected_hash = base_hashes[section.path]
            if section.action == "add":
                if expected_hash is not None:
                    raise AgentError("invalid_request", "Added files require a null base hash")
                validated = self.resolver.resolve(
                    project, section.path, must_exist=False, directory=False, for_write=True
                )
                if validated.target_identity is not None:
                    raise AgentError(
                        "patch_conflict",
                        "Added file already exists",
                        {"conflicts": [{"path": section.path, "reason": "already_exists"}]},
                    )
                raw = _TextFile(list(section.add_lines), os.linesep, True).encode()
                preflight.desired[section.path] = raw
                preflight.validated[section.path] = validated
                preflight.changes.append(_Change("add", section.path, None, None, _sha(raw)))
                continue

            validated = self.resolver.resolve(
                project, section.path, directory=False, for_write=True
            )
            if validated.absolute.stat().st_size > MAX_PATCH_FILE_BYTES:
                raise AgentError("file_too_large", "Patch target exceeds the 8 MiB limit")
            with self.resolver.open_read(project, section.path) as stream:
                old_raw = stream.read(MAX_PATCH_FILE_BYTES + 1)
            if len(old_raw) > MAX_PATCH_FILE_BYTES:
                raise AgentError("file_too_large", "Patch target exceeds the 8 MiB limit")
            old_hash = _sha(old_raw)
            if expected_hash is None or old_hash != expected_hash:
                raise AgentError(
                    "patch_conflict",
                    "The file changed after it was read; read it again and rebuild the patch",
                    {
                        "resolution": "reread_and_rebase",
                        "conflicts": [
                            {
                                "path": section.path,
                                "reason": "base_hash_mismatch",
                                "actual_sha256": old_hash,
                            }
                        ],
                    },
                )
            preflight.validated[section.path] = validated
            if section.action == "delete":
                preflight.desired[section.path] = None
                preflight.changes.append(_Change("delete", section.path, None, old_hash, None))
                continue

            updated = _apply_hunks(_TextFile.decode(old_raw), section.hunks, section.path).encode()
            new_hash = _sha(updated)
            if section.action == "update":
                preflight.desired[section.path] = updated
                preflight.changes.append(_Change("update", section.path, None, old_hash, new_hash))
                continue

            assert section.destination is not None
            if base_hashes[section.destination] is not None:
                raise AgentError("invalid_request", "Move destinations require a null base hash")
            destination = self.resolver.resolve(
                project, section.destination, must_exist=False, directory=False, for_write=True
            )
            if destination.target_identity is not None:
                raise AgentError(
                    "patch_conflict",
                    "Move destination already exists",
                    {"conflicts": [{"path": section.destination, "reason": "already_exists"}]},
                )
            preflight.validated[section.destination] = destination
            preflight.desired[section.path] = None
            preflight.desired[section.destination] = updated
            preflight.changes.append(
                _Change("move", section.path, section.destination, old_hash, new_hash)
            )
        return preflight

    def _commit(
        self,
        project: Any,
        preflight: _Preflight,
        *,
        journal_id: str,
        idempotency_key: str,
        request_digest: str,
        result: dict[str, Any],
        binding: dict[str, str],
        check: Callable[[], None] = _no_check,
    ) -> None:
        # Re-check content preconditions as a complete batch immediately before
        # creating backups or performing any mutation. File identity alone does
        # not detect an in-place write to the same file.
        check()
        self._verify_commit_preconditions(project, preflight)
        check()
        transaction = self.journal_root / journal_id
        backups = transaction / "backups"
        backups.mkdir(parents=True)
        records: list[dict[str, Any]] = []
        mutated = False
        try:
            for index, relative in enumerate(sorted(preflight.desired)):
                check()
                validated = preflight.validated[relative]
                self.resolver.revalidate_for_mutation(project, validated)
                existed = validated.target_identity is not None
                backup_name = f"{index:04d}.bak" if existed else None
                if existed:
                    with self.resolver.open_read(project, relative) as stream:
                        raw = stream.read(MAX_PATCH_FILE_BYTES + 1)
                    expected = next(
                        change.old_hash for change in preflight.changes if change.source == relative
                    )
                    if _sha(raw) != expected:
                        raise AgentError("patch_conflict", "File changed before journal backup")
                    with (backups / str(backup_name)).open("xb") as backup:
                        backup.write(raw)
                        backup.flush()
                        os.fsync(backup.fileno())
                records.append({"path": relative, "existed": existed, "backup": backup_name})
            manifest = {
                "version": 1,
                "project_id": project.project_id,
                "root_fingerprint": project.root_fingerprint,
                "idempotency_key": idempotency_key,
                "request_digest": request_digest,
                "result": result,
                "binding": binding,
                "state": "prepared",
                "records": records,
            }
            registered = self.database.get_project(project.project_id)
            if project.root != registered.root:
                # Local journal only; never synchronize absolute paths to the relay.
                manifest["scope_path"] = str(project.root)
            self._write_manifest(transaction, manifest)
            manifest["state"] = "committing"
            self._write_manifest(transaction, manifest)

            for relative, desired in preflight.desired.items():
                check()
                validated = preflight.validated[relative]
                self.resolver.revalidate_for_mutation(project, validated)
                mutated = True
                if desired is None:
                    validated.absolute.unlink()
                else:
                    self._atomic_replace(validated.absolute, desired)
            check()
            manifest["state"] = "committed"
            self._write_manifest(transaction, manifest)
        except Exception:
            if mutated:
                self._restore(project, transaction, records)
            else:
                shutil.rmtree(transaction, ignore_errors=True)
            raise
        finally:
            manifest_path = transaction / "manifest.json"
            if manifest_path.exists():
                try:
                    state = json.loads(manifest_path.read_text(encoding="utf-8")).get("state")
                except (OSError, json.JSONDecodeError):
                    state = None
                if state == "rolled_back":
                    shutil.rmtree(transaction, ignore_errors=True)

    def _verify_commit_preconditions(self, project: Any, preflight: _Preflight) -> None:
        expected: dict[str, str | None] = {}
        for change in preflight.changes:
            expected[change.source] = change.old_hash
            if change.destination is not None:
                expected[change.destination] = None

        conflicts: list[dict[str, str]] = []
        for relative in sorted(expected):
            expected_hash = expected[relative]
            validated = preflight.validated[relative]
            try:
                self.resolver.revalidate_for_mutation(project, validated)
            except AgentError:
                conflicts.append({"path": relative, "reason": "path_changed"})
                continue

            if expected_hash is None:
                if validated.absolute.exists():
                    conflicts.append({"path": relative, "reason": "already_exists"})
                continue

            try:
                with self.resolver.open_read(project, relative) as stream:
                    digest = hashlib.sha256()
                    while chunk := stream.read(65_536):
                        digest.update(chunk)
                    actual_hash = digest.hexdigest()
            except (AgentError, OSError):
                conflicts.append({"path": relative, "reason": "path_changed"})
                continue
            if actual_hash != expected_hash:
                conflicts.append(
                    {
                        "path": relative,
                        "reason": "base_hash_mismatch",
                        "actual_sha256": actual_hash,
                    }
                )

        if conflicts:
            raise AgentError(
                "patch_conflict",
                "A patch target changed after preflight; read it again and rebuild the patch",
                {"resolution": "reread_and_rebase", "conflicts": conflicts},
            )

    @staticmethod
    def _atomic_replace(target: Path, raw: bytes) -> None:
        temporary = target.with_name(f".{target.name}.codito-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_manifest(transaction: Path, manifest: dict[str, Any]) -> None:
        target = transaction / "manifest.json"
        temporary = transaction / "manifest.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def _restore(self, project: Any, transaction: Path, records: list[dict[str, Any]]) -> None:
        errors: list[str] = []
        for record in reversed(records):
            relative = str(record["path"])
            try:
                current = self.resolver.resolve(
                    project,
                    relative,
                    must_exist=False,
                    directory=False,
                    for_write=True,
                )
                if record["existed"]:
                    backup = transaction / "backups" / str(record["backup"])
                    if not backup.is_file():
                        raise OSError("recovery backup is missing")
                    self._atomic_replace(current.absolute, backup.read_bytes())
                elif current.absolute.exists():
                    current.absolute.unlink()
            except Exception:
                errors.append(relative)
        if errors:
            raise AgentError(
                "recovery_failed",
                "Patch rollback could not restore every file; project was left fail-closed",
                {"paths": errors},
            )
        manifest_path = transaction / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "rolled_back"
            self._write_manifest(transaction, manifest)

    def _recover_transaction(self, transaction: Path, manifest: dict[str, Any]) -> None:
        if manifest.get("state") == "committed":
            binding = dict(manifest["binding"])
            self.database.put_idempotency(
                str(manifest["project_id"]),
                "project_apply_patch",
                str(manifest["idempotency_key"]),
                str(manifest["request_digest"]),
                OperationState.SUCCEEDED,
                dict(manifest["result"]),
                account_id=str(binding["account_id"]),
                grant_id=str(binding["grant_id"]),
                link_id=str(binding["link_id"]),
                device_id=str(binding["device_id"]),
            )
            shutil.rmtree(transaction, ignore_errors=True)
            return
        if manifest.get("state") == "rolled_back":
            self.database.delete_idempotency(
                str(manifest["project_id"]),
                "project_apply_patch",
                str(manifest["idempotency_key"]),
            )
            shutil.rmtree(transaction, ignore_errors=True)
            return
        project = self.database.get_project(str(manifest["project_id"]))
        with resolve_file_target(project, manifest.get("scope_path")) as target:
            if target.project.root_fingerprint != manifest.get("root_fingerprint"):
                raise AgentError("recovery_failed", "Project identity differs from the journal")
            if target.pinned_scope is not None:
                for record in manifest["records"]:
                    target.pinned_scope.directory(str(Path(record["path"]).parent))
                target.pinned_scope.release_for_mutation()
            self._restore(target.project, transaction, list(manifest["records"]))
        self.database.delete_idempotency(
            str(manifest["project_id"]),
            "project_apply_patch",
            str(manifest["idempotency_key"]),
        )
        shutil.rmtree(transaction, ignore_errors=True)

    def recover(self) -> list[str]:
        """Roll back non-terminal local patch journals before accepting requests."""

        recovered: list[str] = []
        for transaction in self.journal_root.iterdir():
            manifest_path = transaction / "manifest.json"
            if not transaction.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self._recover_transaction(transaction, manifest)
                recovered.append(transaction.name)
            except Exception as exc:
                raise AgentError(
                    "recovery_failed",
                    "An incomplete patch journal requires local administrator attention",
                    {"journal_id": transaction.name},
                ) from exc
        return recovered
