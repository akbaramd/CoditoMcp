"""Cooperative native approval routing. This is NOT a shell sandbox/parser.

Native programs can construct paths dynamically, load code or access the network.
The local opt-in warning is mandatory; these checks never claim OS confinement.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from codito_protocol.device_read import normalize_read_scope


def outside_references(
    root: str, cwd: str, command: dict[str, Any], declared: list[str]
) -> tuple[str, ...]:
    project = PureWindowsPath(root)
    values = [cwd, *(normalize_read_scope(path) for path in declared)]
    # Literal paths are a usability safety net. Do not use this as a proof that
    # unlisted paths cannot be accessed by scripts, tools or child processes.
    texts = [str(command.get("script", "")), str(command.get("executable", ""))]
    texts.extend(str(value) for value in command.get("arguments", []))
    environment = command.get("environment", {})
    if isinstance(environment, dict):
        texts.extend(str(value) for value in environment.values())
    external: set[str] = set()
    for text in texts:
        values.extend(re.findall(r"[A-Za-z]:[/\\][^\s'\";|<>]*", text))
        values.extend(re.findall(r"['\"]([A-Za-z]:[/\\][^'\"]+)['\"]", text))
        if re.search(r"(^|[\s'\"=])\.\.([/\\]|$)", text):
            external.add("parent-relative reference (review exact command)")
        if "\\\\" in text or re.search(r"[A-Za-z]:(?![/\\])", text):
            external.add("UNC/device/drive-relative reference (review exact command)")
        if re.search(r"(?:^|[\s'\"=])[/\\](?![/\\])", text):
            external.add("root-relative reference (review exact command)")
    for value in values:
        path = PureWindowsPath(value)
        if not path.is_relative_to(project) or ".." in path.parts:
            external.add(str(path))
    return tuple(sorted(external))


def uncertain_constructs(command: dict[str, Any]) -> tuple[str, ...]:
    """Prompt for recognized indirection; not a complete shell security analysis.

    Ordinary structured build tools intentionally remain usable without prompts
    in Project access. Their scripts/plugins/children still run as the local user
    and cannot be proven project-confined by inspecting command-line text.
    """
    reasons: set[str] = set()
    if command.get("kind") == "script":
        script = str(command.get("script", ""))
        if re.search(r"[$%`^]", script):
            reasons.add("variable expansion or escaping may construct external access")
        if re.search(r"[;&|<>(){}\r\n]", script):
            reasons.add("compound shell syntax requires local review")
        if re.search(
            r"\b(?:invoke-expression|iex|start-process|new-object|set-location|pushd|popd|call|start)\b",
            script,
            re.IGNORECASE,
        ):
            reasons.add("indirect execution or working-directory change requires review")
        if "~" in script:
            reasons.add("home-directory expansion may access files outside the project")
    elif command.get("kind") == "exec":
        executable = PureWindowsPath(str(command.get("executable", ""))).stem.casefold()
        if executable in {
            "cmd",
            "powershell",
            "pwsh",
            "python",
            "python3",
            "py",
            "node",
            "perl",
            "ruby",
            "wscript",
            "cscript",
            "mshta",
            "bash",
            "sh",
        }:
            arguments = [str(value).casefold() for value in command.get("arguments", [])]
            if any(
                value in {"/c", "/k", "-c", "-e", "-command", "-encodedcommand", "-enc"}
                for value in arguments
            ):
                reasons.add("interpreter inline code requires local review")
    return tuple(sorted(reasons))


def executable_identity(specification: dict[str, Any]) -> dict[str, Any]:
    """Snapshot broker-compatible resolution for exact saved-command consent.

    This is rechecked immediately before launch, but the broker still opens the
    executable by path; it is not a handle-pinned executable security boundary.
    """
    command = specification["command"]
    environment = {
        str(key).upper(): str(value) for key, value in specification["environment"].items()
    }
    if command.get("kind") == "script":
        system_root = Path(environment.get("SYSTEMROOT", r"C:\Windows"))
        candidates = [
            system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if command.get("shell") == "powershell"
            else system_root / "System32" / "cmd.exe"
        ]
    else:
        executable = str(command.get("executable", ""))
        if PureWindowsPath(executable).is_absolute():
            bases = [Path(executable)]  # The broker independently refuses absolute inputs.
        elif "/" in executable or "\\" in executable:
            bases = [Path(specification["project_root"]) / executable]
        else:
            bases = [
                Path(directory) / executable
                for directory in environment.get("PATH", "").split(os.pathsep)
                if directory
            ]
        extensions = (
            [""]
            if PureWindowsPath(executable).suffix
            else environment.get("PATHEXT", ".EXE;.COM").split(";")
        )
        candidates = [
            Path(str(candidate) + extension)
            for candidate in bases
            for extension in extensions
            if extension or PureWindowsPath(executable).suffix
        ]
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            info = candidate.stat()
        except OSError:
            continue
        return {
            "path": str(candidate.absolute()).casefold(),
            "device": info.st_dev,
            "file_id": info.st_ino,
            "size": info.st_size,
            "modified_ns": info.st_mtime_ns,
        }
    return {"unresolved": True, "executor": command.get("executable", command.get("shell"))}


def permission_scope(cwd: str, references: tuple[str, ...]) -> str:
    # Exact request scope, not a drive-wide wildcard; normalize Windows case.
    return json.dumps(
        {
            "cwd": str(PureWindowsPath(cwd)).casefold(),
            "references": [p.casefold() for p in references],
        },
        sort_keys=True,
    )
