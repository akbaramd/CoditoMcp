from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from codito_protocol import TOOL_CONTRACTS
from django.conf import settings
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from .authz import AuthorizationFailure, MCPPrincipal, authenticate_mcp
from .diagnostics import emit
from .dispatch import ToolDispatchError, dispatch_tool, error_tool_result, success_tool_result

mcp = MCPServer(
    "Codito",
    instructions=(
        "Operate only on opaque projects registered to this device. Local approval and trust "
        "are enforced by the Windows agent and cannot be asserted in tool inputs."
    ),
)


def _principal(context: Context) -> MCPPrincipal:
    request_context = context.request_context
    request = getattr(request_context, "request", None)
    if request is None:
        raise ToolDispatchError(
            "authorization_context_missing", "MCP request context is unavailable"
        )
    state = request.scope.get("state", {})
    principal = state.get("codito_principal")
    if not isinstance(principal, MCPPrincipal):
        raise ToolDispatchError(
            "authorization_context_missing", "MCP authorization binding is unavailable"
        )
    return principal


async def _run(context: Context, name: str, arguments: dict[str, Any]) -> CallToolResult:
    principal: MCPPrincipal | None = None
    meta: dict[str, Any] | None = None
    try:
        principal = _principal(context)
        receipt = await dispatch_tool(principal, name, arguments)
        result = success_tool_result(receipt)
    except ToolDispatchError as exc:
        result = error_tool_result(exc)
        if exc.code == "insufficient_scope" and principal is not None:
            # A tool error is HTTP 200, so the gateway's HTTP 401 challenge is
            # never reached. ChatGPT needs this result-level challenge to relink
            # the current connection instead of repeatedly using its old grant.
            required = set(exc.details["required_scopes"])
            allowed = set(settings.MCP_TOOL_SCOPES) | {"openid", "profile"}
            scopes = " ".join(sorted((principal.scopes | required) & allowed))
            metadata_url = (
                f"{settings.PUBLIC_BASE_URL}/.well-known/oauth-protected-resource"
                f"/mcp/d/{principal.link_id}"
            )
            meta = {
                "mcp/www_authenticate": [
                    f'Bearer resource_metadata="{metadata_url}", '
                    'error="insufficient_scope", '
                    'error_description="Reconnect this Codito connection to grant the '
                    'requested permission", '
                    f'scope="{scopes}"'
                ]
            }
            emit(
                "mcp.scope_challenge",
                tool=name,
                token_record_id=principal.access_token_id,
                required_scopes=sorted(required),
                missing_scopes=sorted(required - principal.scopes),
            )
    return CallToolResult(
        content=[
            ImageContent(**item) if item["type"] == "image" else TextContent(**item)
            for item in result["content"]
        ],
        structuredContent=result["structuredContent"],
        isError=result["isError"],
        meta=meta,
    )


async def project_read(
    context: Context,
    operation: Literal["list_projects", "list_directory", "read_file", "search_text"],
    project_id: str | None = None,
    path: str = "",
    glob: str = "*",
    recursive: bool = False,
    cursor: str | None = None,
    continuation: str | None = None,
    limit: int = 50,
    start_line: int = 1,
    end_line: int | None = None,
    max_bytes: int = 262_144,
    query: str | None = None,
    case_sensitive: bool = False,
    regex: bool = False,
    max_results: int = 100,
    max_bytes_per_match: int = 2048,
) -> CallToolResult:
    common = {"operation": operation}
    if operation == "list_projects":
        arguments = {**common, "cursor": cursor, "limit": limit}
    elif operation == "list_directory":
        arguments = {
            **common,
            "project_id": project_id,
            "path": path,
            "glob": glob,
            "recursive": recursive,
            "cursor": cursor,
            "limit": limit,
        }
    elif operation == "read_file":
        arguments = {
            **common,
            "project_id": project_id,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "max_bytes": max_bytes,
            "continuation": continuation,
        }
    else:
        arguments = {
            **common,
            "project_id": project_id,
            "query": query,
            "path": path,
            "glob": glob,
            "case_sensitive": case_sensitive,
            "regex": regex,
            "max_results": max_results,
            "max_bytes_per_match": max_bytes_per_match,
            "continuation": continuation,
        }
    arguments = {key: value for key, value in arguments.items() if value is not None}
    return await _run(context, "project_read", arguments)


async def project_apply_patch(
    context: Context,
    project_id: str,
    patch: str,
    base_hashes: dict[str, str | None],
    idempotency_key: str,
    dry_run: bool = False,
) -> CallToolResult:
    return await _run(
        context,
        "project_apply_patch",
        {
            "project_id": project_id,
            "patch": patch,
            "base_hashes": base_hashes,
            "idempotency_key": idempotency_key,
            "dry_run": dry_run,
        },
    )


async def project_shell(
    context: Context,
    action: Literal["start", "poll", "cancel"],
    project_id: str,
    job_id: str | None = None,
    working_directory: str = "",
    purpose: str | None = None,
    timeout_seconds: int = 300,
    output_limit_bytes: int = 2 * 1024 * 1024,
    idempotency_key: str | None = None,
    command: dict[str, Any] | None = None,
    sequence_cursor: int = 0,
    wait_milliseconds: int = 0,
    reason: str = "cancelled by caller",
    execution: Literal["project_policy", "native_approval"] = "project_policy",
    external_working_directory: str | None = None,
    requested_external_paths: list[str] | None = None,
    approval_timeout_seconds: int = 180,
) -> CallToolResult:
    if action == "start":
        arguments = {
            "action": action,
            "project_id": project_id,
            "execution": execution,
            "external_working_directory": external_working_directory,
            "requested_external_paths": requested_external_paths or [],
            "approval_timeout_seconds": approval_timeout_seconds,
            "working_directory": working_directory,
            "purpose": purpose,
            "timeout_seconds": timeout_seconds,
            "output_limit_bytes": output_limit_bytes,
            "idempotency_key": idempotency_key,
            "command": command,
        }
    elif action == "poll":
        arguments = {
            "action": action,
            "project_id": project_id,
            "job_id": job_id,
            "sequence_cursor": sequence_cursor,
            "wait_milliseconds": wait_milliseconds,
        }
    else:
        arguments = {"action": action, "project_id": project_id, "job_id": job_id, "reason": reason}
    arguments = {key: value for key, value in arguments.items() if value is not None}
    return await _run(context, "project_shell", arguments)


async def project_manage(
    context: Context,
    operation: Literal["get_projects", "request_add_project", "rename_project", "remove_project"],
    project_id: str | None = None,
    title: str | None = None,
    idempotency_key: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> CallToolResult:
    if operation == "get_projects":
        arguments = {"operation": operation, "cursor": cursor, "limit": limit}
    elif operation == "request_add_project":
        arguments = {
            "operation": operation,
            "title": title,
            "idempotency_key": idempotency_key,
        }
    elif operation == "rename_project":
        arguments = {
            "operation": operation,
            "project_id": project_id,
            "title": title,
            "idempotency_key": idempotency_key,
        }
    else:
        arguments = {
            "operation": operation,
            "project_id": project_id,
            "idempotency_key": idempotency_key,
        }
    return await _run(
        context,
        "project_manage",
        {key: value for key, value in arguments.items() if value is not None},
    )


async def device_read(
    context: Context,
    operation: Literal["list_directory", "read_file"],
    scope_path: str,
    purpose: str,
    path: str = "",
    offset: int = 0,
    limit: int = 100,
    start_line: int = 1,
    max_lines: int = 200,
    max_bytes: int = 65536,
) -> CallToolResult:
    return await _run(
        context,
        "device_read",
        {
            "operation": operation,
            "scope_path": scope_path,
            "purpose": purpose,
            "path": path,
            "offset": offset,
            "limit": limit,
            "start_line": start_line,
            "max_lines": max_lines,
            "max_bytes": max_bytes,
        },
    )


async def device_screenshot(
    context: Context,
    purpose: str,
    display: str = "primary",
    max_dimension: int = 1600,
    action: Literal["capture", "list_displays"] = "capture",
) -> CallToolResult:
    return await _run(
        context,
        "device_screenshot",
        {"action": action, "purpose": purpose, "display": display, "max_dimension": max_dimension},
    )


async def device_desktop(
    context: Context,
    url: str,
    purpose: str,
    action: Literal["open_browser"] = "open_browser",
    browser: Literal["default", "firefox"] = "default",
) -> CallToolResult:
    return await _run(
        context,
        "device_desktop",
        {"action": action, "url": url, "purpose": purpose, "browser": browser},
    )


def _register_tool(name: str, function: Any) -> None:
    contract = TOOL_CONTRACTS[name]
    annotations = ToolAnnotations(**contract["annotations"])
    kwargs: dict[str, Any] = {
        "name": name,
        "description": contract["description"],
        "annotations": annotations,
    }
    if "meta" in inspect.signature(mcp.tool).parameters:
        kwargs["meta"] = {"securitySchemes": contract["securitySchemes"]}
    mcp.tool(**kwargs)(function)


_register_tool("project_read", project_read)
_register_tool("project_apply_patch", project_apply_patch)
_register_tool("project_shell", project_shell)
_register_tool("project_manage", project_manage)
_register_tool("device_read", device_read)
_register_tool("device_screenshot", device_screenshot)
_register_tool("device_desktop", device_desktop)

mcp_http_app = mcp.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
    host="0.0.0.0",  # noqa: S104 - container edge binds intentionally; firewall/proxy own exposure.
)


class DeviceMCPGateway:
    """Authenticate a dynamic device resource before entering the stateless MCP SDK app."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            response = JSONResponse({"error": "not_found"}, status_code=404)
            await response(scope, receive, send)
            return
        request_path = scope.get("path", "")
        mounted_root = scope.get("root_path", "")
        if mounted_root and request_path.startswith(mounted_root):
            request_path = request_path[len(mounted_root) :]
        link_id = request_path.strip("/")
        if not link_id or "/" in link_id:
            response = JSONResponse({"error": "not_found"}, status_code=404)
            await response(scope, receive, send)
            return
        headers = Headers(scope=scope)
        try:
            principal = await authenticate_mcp(
                {key.lower(): value for key, value in headers.items()}, link_id
            )
        except AuthorizationFailure as exc:
            from .diagnostics import emit, safe_error

            emit("mcp.authorization_rejected", error=safe_error(exc.code), status=exc.status)
            metadata_url = (
                f"{settings.PUBLIC_BASE_URL}/.well-known/oauth-protected-resource/mcp/d/{link_id}"
            )
            response = JSONResponse(
                {"error": exc.code, "error_description": exc.description},
                status_code=exc.status,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{metadata_url}", error="{exc.code}", '
                        f'scope="{" ".join(settings.MCP_TOOL_SCOPES)}"'
                    )
                },
            )
            await response(scope, receive, send)
            return
        child_scope = dict(scope)
        child_scope["path"] = "/"
        child_scope["raw_path"] = b"/"
        child_scope["root_path"] = f"/mcp/d/{link_id}"
        child_scope["state"] = {**scope.get("state", {}), "codito_principal": principal}
        await self.app(child_scope, receive, send)


@asynccontextmanager
async def mcp_lifespan(app: Any) -> AsyncIterator[None]:
    async with mcp.session_manager.run():
        yield
