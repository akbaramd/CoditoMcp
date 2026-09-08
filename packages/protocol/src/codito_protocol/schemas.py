"""JSON Schema adapters and deterministic schema export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from .desktop_action import DeviceDesktopInput, DeviceDesktopResult
from .device_read import DeviceReadInput, DeviceReadResult
from .envelope import TunnelEnvelope
from .errors import ToolError
from .manage import ProjectManageInput, ProjectManageResult
from .patch import ProjectApplyPatchInput, ProjectApplyPatchResult
from .read import ProjectReadInput, ProjectReadResult
from .screenshot import DeviceScreenshotInput, ScreenshotToolResult
from .shell import ProjectShellInput, ProjectShellResult

_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "device-desktop-input": TypeAdapter(DeviceDesktopInput),
    "device-desktop-result": TypeAdapter(DeviceDesktopResult),
    "device-screenshot-input": TypeAdapter(DeviceScreenshotInput),
    "device-screenshot-result": TypeAdapter(ScreenshotToolResult),
    "device-read-input": TypeAdapter(DeviceReadInput),
    "device-read-result": TypeAdapter(DeviceReadResult),
    "project-read-input": TypeAdapter(ProjectReadInput),
    "project-read-result": TypeAdapter(ProjectReadResult),
    "project-apply-patch-input": TypeAdapter(ProjectApplyPatchInput),
    "project-apply-patch-result": TypeAdapter(ProjectApplyPatchResult),
    "project-shell-input": TypeAdapter(ProjectShellInput),
    "project-shell-result": TypeAdapter(ProjectShellResult),
    "project-manage-input": TypeAdapter(ProjectManageInput),
    "project-manage-result": TypeAdapter(ProjectManageResult),
    "tunnel-envelope": TypeAdapter(TunnelEnvelope),
    "tool-error": TypeAdapter(ToolError),
}


def schema_for(name: str) -> dict[str, Any]:
    try:
        adapter = _ADAPTERS[name]
    except KeyError as exc:
        raise KeyError(f"unknown Codito schema: {name}") from exc
    schema = adapter.json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://codito.akbaramd.ir/schemas/v1/{name}.schema.json"
    return schema


def export_schemas(destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in sorted(_ADAPTERS):
        path = destination / f"{name}.schema.json"
        path.write_text(
            json.dumps(schema_for(name), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        written.append(path)
    return written


def validate_project_read(value: Any) -> ProjectReadInput:
    return TypeAdapter(ProjectReadInput).validate_python(value)


def validate_project_apply_patch(value: Any) -> ProjectApplyPatchInput:
    return TypeAdapter(ProjectApplyPatchInput).validate_python(value)


def validate_project_shell(value: Any) -> ProjectShellInput:
    return TypeAdapter(ProjectShellInput).validate_python(value)


def validate_project_manage(value: Any) -> ProjectManageInput:
    return TypeAdapter(ProjectManageInput).validate_python(value)
