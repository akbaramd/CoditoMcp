"""MCP-facing names, descriptions, security schemes, and annotations."""

from __future__ import annotations

from typing import Any, Final


def _oauth_scheme(*scopes: str) -> dict[str, Any]:
    return {"type": "oauth2", "scopes": list(scopes)}


TOOL_CONTRACTS: Final[dict[str, dict[str, Any]]] = {
    "project_read": {
        "title": "Read registered projects",
        "description": (
            "List registered projects, list a project-relative directory, read a bounded "
            "range of a file, or search text. Absolute local paths are never accepted or returned."
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
            "Execution mode and local approval are controlled only by device policy."
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
