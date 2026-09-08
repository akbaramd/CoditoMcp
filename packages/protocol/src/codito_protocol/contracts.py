"""MCP-facing names, descriptions, security schemes, and annotations."""

from __future__ import annotations

from typing import Any, Final


def _oauth_scheme(*scopes: str) -> dict[str, Any]:
    return {"type": "oauth2", "scopes": list(scopes)}


TOOL_CONTRACTS: Final[dict[str, dict[str, Any]]] = {
    "device_desktop": {
        "title": "Open a URL in the Windows browser with local consent",
        "description": (
            "Ask Windows to open an HTTP/HTTPS URL in the default browser or installed Firefox. "
            "Requires a separate one-shot Windows approval showing the exact browser and URL. "
            "Returns submitted, not proof of page load. Existing browser profile/cookies "
            "may be used. Cannot click, type, launch arbitrary apps, or use custom URI schemes. "
            "No automatic replay after an uncertain open. Use device_screenshot separately "
            "to inspect the display after the page loads."
        ),
        "required_scopes": ["shell:execute"],
        "securitySchemes": [_oauth_scheme("shell:execute")],
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    "device_screenshot": {
        "title": "Capture a selected Windows display with local consent",
        "description": (
            "Use action=list_displays to discover opaque display IDs without capturing pixels, "
            "then action=capture with display='primary' or a returned screen ID. "
            "Returns an actual PNG image for visual inspection, not a filesystem path. "
            "Requires separate Windows screen consent; file/shell permissions do not authorize it. "
            "Shows all visible selected-display windows, potentially including private data. "
            "Windows offers Deny, Allow, or Always allow for the same selected monitor, "
            "account and connection. Saved screen permission is revoked in local Settings; "
            "monitor/layout changes require new consent. "
            "Cannot capture the lock screen/UAC secure desktop or control mouse/keyboard. "
            "Image is scaled within max_dimension and 600 KB; repeat for a new capture."
        ),
        "required_scopes": ["screen:read"],
        "securitySchemes": [_oauth_scheme("screen:read")],
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    "device_read": {
        "title": "Request Windows read access outside projects",
        "description": (
            "When the user asks to inspect a local directory or drive outside registered "
            "projects, use this tool to request Windows approval instead of refusing or "
            "registering the whole drive. Supply scope_path (e.g. C:/), a relative path, "
            "and purpose. Windows offers Deny, Allow once, or Always allow reading this "
            "directory and descendants for this connection identity. Only the local user "
            "can approve. Saved consent never permits edits or shell execution. Reads are "
            "bounded; directories are non-recursive. Permission-denied and reparse paths "
            "remain blocked. Choose the narrowest directory the user requested."
        ),
        "required_scopes": ["files:read"],
        "securitySchemes": [_oauth_scheme("files:read")],
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    "project_read": {
        "title": "Read registered projects",
        "description": (
            "List registered projects, list a project-relative directory, read a bounded "
            "range of a file, or search text. This tool accepts only project-relative paths. "
            "For user-requested reads outside registered projects, use device_read, which "
            "requests Windows consent."
        ),
        "required_scopes_by_operation": {
            "list_projects": ["projects:read"],
            "list_directory": ["projects:read", "files:read"],
            "read_file": ["projects:read", "files:read"],
            "search_text": ["projects:read", "files:read"],
        },
        # Tool-level metadata is conservative because MCP cannot advertise scopes
        # per discriminated operation. The server permits list_projects with only
        # projects:read after it inspects the selected operation.
        "securitySchemes": [_oauth_scheme("projects:read", "files:read")],
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    "project_apply_patch": {
        "title": "Apply an anchored project patch",
        "description": (
            "Preflight and atomically apply an exact Begin Patch document against required "
            "base hashes. Context is exact; fuzzy or ambiguous hunks are rejected."
        ),
        "required_scopes": ["projects:read", "files:read", "files:write"],
        "securitySchemes": [_oauth_scheme("projects:read", "files:read", "files:write")],
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    "project_shell": {
        "title": "Run or manage a bounded project command",
        "description": (
            "Start, poll, or cancel a non-interactive command in a registered project. "
            "For installed Windows tools (uv, dotnet, git, etc.) or access outside the "
            "project, set execution=native_approval to request one-shot Windows consent "
            "for full logged-in-user authority and the host tool PATH. A script may change "
            "directory after approval. Never claim that a working directory confines native "
            "commands. Missing tools or failed isolation never silently bypass consent. "
            "Default project_policy keeps locally selected isolation/trust settings. "
            "In native_project mode, ordinary project commands use installed Windows tools "
            "without prompts. For outside-project work set external_working_directory "
            "and declare requested_external_paths; Windows approval is required. "
            "Start returns pending_approval immediately; poll that job while the user decides, "
            "do not resubmit a new start. Approval timeout is separate from process timeout. "
            "Native project checks declared and literal paths, NOT arbitrary program behavior."
        ),
        "required_scopes": ["projects:read", "shell:execute"],
        "securitySchemes": [_oauth_scheme("projects:read", "shell:execute")],
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    "project_manage": {
        "title": "Manage registered projects",
        "description": (
            "List, request local registration, rename, or unregister a project. Local paths are "
            "never accepted; registration and destructive metadata changes require Windows UI."
        ),
        "required_scopes_by_operation": {
            "get_projects": ["projects:read"],
            "request_add_project": ["projects:read", "projects:write"],
            "rename_project": ["projects:read", "projects:write"],
            "remove_project": ["projects:read", "projects:write"],
        },
        "securitySchemes": [_oauth_scheme("projects:read", "projects:write")],
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
}
