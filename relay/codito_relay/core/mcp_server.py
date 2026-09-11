from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from codito_protocol import TOOL_CONTRACTS
from codito_protocol.facade import FACADE_MODELS, facade_wire_request
from codito_protocol.facade_contracts import (
    FACADE_CONTRACTS,
    FACADE_OUTPUT_MODELS,
    MCP_SERVER_INSTRUCTIONS,
)
from django.conf import settings
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import (
    CallToolResult,
    ImageContent,
    InputRequiredResult,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from codito_relay import __version__

from .authz import AuthorizationFailure, MCPPrincipal, authenticate_mcp
from .diagnostics import emit
from .dispatch import ToolDispatchError, dispatch_tool, error_tool_result, success_tool_result
from .redis_async import close_shared_redis


class CoditoMCPServer(MCPServer[Any]):
    """Public SDK extension points keep cached aliases callable, but not listed."""

    async def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name=name,
                title=contract["title"],
                description=contract["description"],
                input_schema=FACADE_MODELS[name].model_json_schema(),
                output_schema=FACADE_OUTPUT_MODELS[name].model_json_schema(),
                annotations=ToolAnnotations(**contract["annotations"]),
                meta={
                    "securitySchemes": contract["securitySchemes"],
                    "openai/toolInvocation/invoking": contract["invoking"],
                    "openai/toolInvocation/invoked": contract["invoked"],
                },
            )
            for name, contract in FACADE_CONTRACTS.items()
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        if name in FACADE_MODELS:
            return await _run(context or Context(mcp_server=self), name, arguments)
        return await super().call_tool(name, arguments, context)


mcp = CoditoMCPServer(
    "Codito",
    version=__version__,
    instructions=MCP_SERVER_INSTRUCTIONS,
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
        wire_name = name
        if name in FACADE_MODELS:
            try:
                wire_name, arguments = facade_wire_request(name, arguments)
            except ValidationError as exc:
                raise ToolDispatchError(
                    "invalid_request",
                    "Tool input failed its focused contract; inspect the tool schema",
                    details={
                        "fields": sorted(
                            {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
                        )
                    },
                ) from exc
        receipt = await dispatch_tool(principal, wire_name, arguments)
        result = success_tool_result(receipt)
        if name in FACADE_MODELS and "ok" not in result["structuredContent"]:
            # The legacy local project-list result predates the common envelope.
            result["structuredContent"] = {
                "operation_id": receipt.operation_id,
                "ok": True,
                "text": result["content"][0]["text"],
                "result": receipt.result,
            }
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
    if name in FACADE_OUTPUT_MODELS:
        try:
            validated_output = FACADE_OUTPUT_MODELS[name].model_validate(
                result["structuredContent"]
            )
            result["structuredContent"] = validated_output.model_dump(
                mode="json", exclude_none=True
            )
        except ValidationError:
            emit("mcp.output_validation_failed", tool=name)
            result = error_tool_result(
                ToolDispatchError(
                    "internal_error",
                    "Tool result failed its declared output schema",
                    retryable=False,
                )
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
    max_output_bytes: int = 256 * 1024,
    reason: str = "cancelled by caller",
    execution: Literal["project_policy", "native_approval"] = "project_policy",
    external_working_directory: str | None = None,
    requested_external_paths: list[str] | None = None,
    approval_timeout_seconds: int = 180,
    start_wait_milliseconds: int = 30_000,
) -> CallToolResult:
    if action == "start":
        arguments = {
            "action": action,
            "project_id": project_id,
            "execution": execution,
            "external_working_directory": external_working_directory,
            "requested_external_paths": requested_external_paths or [],
            "approval_timeout_seconds": approval_timeout_seconds,
            "start_wait_milliseconds": start_wait_milliseconds,
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
            "max_output_bytes": max_output_bytes,
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


async def project_frontend(
    context: Context,
    operation: Literal["session_start", "snapshot", "inspect", "act", "source", "session_stop"],
    project_id: str,
    purpose: str,
    session_id: str | None = None,
    route: str = "/",
    viewport: dict[str, int] | None = None,
    ready_timeout_seconds: int = 30,
    max_elements: int = 500,
    snapshot_id: str | None = None,
    element_id: str | None = None,
    x: int | None = None,
    y: int | None = None,
    action: Literal["click", "hover", "focus", "fill", "press", "scroll", "select"] | None = None,
    text: str | None = None,
    key: str | None = None,
    option: str | None = None,
    delta_x: int | None = None,
    delta_y: int | None = None,
    idempotency_key: str | None = None,
    reason: str = "Frontend review complete",
) -> CallToolResult:
    common: dict[str, Any] = {
        "operation": operation,
        "project_id": project_id,
        "purpose": purpose,
    }
    if operation == "session_start":
        arguments = {
            **common,
            "route": route,
            "viewport": viewport,
            "ready_timeout_seconds": ready_timeout_seconds,
            "idempotency_key": idempotency_key,
        }
    elif operation == "snapshot":
        arguments = {**common, "session_id": session_id, "max_elements": max_elements}
    elif operation == "inspect":
        arguments = {
            **common,
            "session_id": session_id,
            "snapshot_id": snapshot_id,
            "element_id": element_id,
            "x": x,
            "y": y,
        }
    elif operation == "act":
        arguments = {
            **common,
            "session_id": session_id,
            "snapshot_id": snapshot_id,
            "element_id": element_id,
            "action": action,
            "text": text,
            "key": key,
            "option": option,
            "delta_x": delta_x,
            "delta_y": delta_y,
            "idempotency_key": idempotency_key,
        }
    elif operation == "source":
        arguments = {
            **common,
            "session_id": session_id,
            "snapshot_id": snapshot_id,
            "element_id": element_id,
        }
    else:
        arguments = {**common, "session_id": session_id, "reason": reason}
    return await _run(
        context,
        "project_frontend",
        {key: value for key, value in arguments.items() if value is not None},
    )


def _register_tool(name: str, function: Any) -> None:
    contract = TOOL_CONTRACTS[name]
    annotations = ToolAnnotations(**contract["annotations"])
    kwargs: dict[str, Any] = {
        "name": name,
        "title": contract["title"],
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
_register_tool("project_frontend", project_frontend)

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
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        await close_shared_redis()
