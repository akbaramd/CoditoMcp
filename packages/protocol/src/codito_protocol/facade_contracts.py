"""Descriptors for the focused public tool surface; old names remain wire aliases."""

from __future__ import annotations

from typing import Any

from .errors import ToolError
from .types import CoditoModel


class FacadeToolResult(CoditoModel):
    ok: bool
    text: str
    operation_id: str | None = None
    result: dict[str, Any] | None = None
    error: ToolError | None = None


def _contract(
    title: str,
    description: str,
    scopes: list[str],
    invoking: str,
    invoked: str,
    *,
    read: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "securitySchemes": [{"type": "oauth2", "scopes": scopes}],
        "annotations": {
            "readOnlyHint": read,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": open_world,
        },
        "invoking": invoking,
        "invoked": invoked,
    }


_READ = ["projects:read", "files:read"]
_WRITE = [*_READ, "files:write"]
_MANAGE = ["projects:read", "projects:write"]
_SHELL = ["projects:read", "shell:execute"]
_SCREEN = ["projects:read", "screen:read"]
_SCOPE = (
    "Paths are relative to the origin project, or to an explicit requested scope_path. "
    "Each call uses that project's locally configured policy; scope_path never proves approval. "
)

FACADE_CONTRACTS: dict[str, dict[str, Any]] = {
    "projects_list": _contract(
        "List projects",
        "List this device's registered projects and their opaque IDs.",
        ["projects:read"],
        "Listing projects…",
        "Project list ready",
        read=True,
        idempotent=True,
    ),
    "project_add": _contract(
        "Add project",
        "Request a project title; the Windows user selects the local folder. "
        "Returns pending_local_selection, not a registered path.",
        _MANAGE,
        "Requesting project registration…",
        "Project registration response ready",
        idempotent=True,
    ),
    "project_rename": _contract(
        "Rename project",
        "Rename registered project metadata under local policy; files are unchanged.",
        _MANAGE,
        "Requesting project rename…",
        "Project rename response ready",
        idempotent=True,
    ),
    "project_remove": _contract(
        "Remove project",
        "Unregister a project under local policy; this does not delete its files.",
        _MANAGE,
        "Requesting project removal…",
        "Project removal response ready",
        destructive=True,
        idempotent=True,
    ),
    "directory_list": _contract(
        "List directory",
        _SCOPE + "List bounded file/directory metadata with glob and cursor pagination.",
        _READ,
        "Listing directory…",
        "Directory response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "file_read": _contract(
        "Read file",
        _SCOPE + "Read a bounded numbered text range with encoding, newlines and SHA-256. "
        "Use the returned hash as a mutation precondition; follow continuation for more lines.",
        _READ,
        "Reading file…",
        "File response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "text_search": _contract(
        "Search file text",
        _SCOPE + "Search bounded text/globs; returns matching paths, lines and continuation.",
        _READ,
        "Searching file text…",
        "Search response ready",
        read=True,
        idempotent=True,
        open_world=True,
    ),
    "file_patch": _contract(
        "Patch files",
        _SCOPE + "Apply an exact anchored *** Begin Patch document with Add/Update/Delete/Move "
        "sections. Supply every touched path's base hash (null only for new files). "
        "No fuzzy matching; the full batch is preflighted and journaled. "
        "dry_run performs no writes.",
        _WRITE,
        "Checking and applying patch…",
        "Patch response ready",
        destructive=True,
        idempotent=True,
        open_world=True,
    ),
    "file_delete": _contract(
        "Delete file",
        _SCOPE + "Delete exactly one file against its current SHA-256. "
        "Uses the same journaled patch engine; cannot recursively remove directories. "
        "Read first if the current base_hash is unknown. dry_run performs no deletion.",
        _WRITE,
        "Checking file deletion…",
        "File deletion response ready",
        destructive=True,
        idempotent=True,
        open_world=True,
    ),
    "execute_shell": _contract(
        "Execute Windows command",
        "Run command as a noninteractive PowerShell or cmd script. "
        "Set executor, cwd and timeout_seconds directly. Uses the chosen project's local authority "
        "and installed Windows tools when policy permits; outside paths/cwd may require consent. "
        "Native cwd is not confinement. No PTY, elevation or automatic uncertain replay. "
        "If pending_approval, queued or running, continue with shell_status using the same job_id; "
        "do not resubmit command or wait for another user message just to poll.",
        _SHELL,
        "Running Windows command…",
        "Shell response ready",
        destructive=True,
        open_world=True,
    ),
    "shell_status": _contract(
        "Read shell status",
        "Poll the existing job's state and sequenced output. "
        "Reuse project_id/job_id and the returned sequence cursor; keep polling nonterminal jobs.",
        _SHELL,
        "Waiting for shell output…",
        "Shell status ready",
        read=True,
        idempotent=True,
    ),
    "shell_cancel": _contract(
        "Cancel shell job",
        "Request cancellation of an existing bounded process tree; "
        "use the original project_id/job_id. This is not a new command.",
        _SHELL,
        "Cancelling shell job…",
        "Cancellation response ready",
        destructive=True,
        idempotent=True,
    ),
    "screen_list": _contract(
        "List screens",
        "List opaque display IDs and topology metadata without capturing pixels.",
        ["screen:read"],
        "Listing Windows screens…",
        "Screen list ready",
        read=True,
    ),
    "screenshot_capture": _contract(
        "Capture screenshot",
        "Capture one selected Windows display as an actual PNG image. "
        "Use display from screen_list, or primary. Applies the origin project's screen policy; "
        "when consent is needed the local user can allow once or save same-monitor permission. "
        "Visible private windows can be included. Lock/UAC desktops are blocked. "
        "This does not authorize files, shell, mouse or keyboard control.",
        _SCREEN,
        "Capturing selected screen…",
        "Screenshot response ready",
        read=True,
        open_world=True,
    ),
    "browser_open": _contract(
        "Open browser",
        "Open a requested HTTP/HTTPS URL in the default browser or installed Firefox "
        "under the origin project's desktop policy. Existing browser cookies/profile may be used. "
        "Returns submitted, never proof the page loaded. No arbitrary apps, URI schemes, clicking "
        "or typing. Use screenshot_capture separately to inspect; never replay uncertain opens.",
        _SHELL,
        "Opening browser…",
        "Browser request response ready",
        destructive=True,
        open_world=True,
    ),
}
