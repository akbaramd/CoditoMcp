"""Cooperative native approval routing. This is NOT a shell sandbox/parser.

Native programs can construct paths dynamically, load code or access the network.
The local opt-in warning is mandatory; these checks never claim OS confinement.
"""

from __future__ import annotations

import json
import re
from pathlib import PureWindowsPath
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
    external: set[str] = set()
    for text in texts:
        values.extend(re.findall(r"[A-Za-z]:[/\\][^\s'\";|<>]*", text))
        values.extend(re.findall(r"['\"]([A-Za-z]:[/\\][^'\"]+)['\"]", text))
        if re.search(r"(^|[\s'\"=])\.\.([/\\]|$)", text):
            external.add("parent-relative reference (review exact command)")
        if "\\\\" in text or re.search(r"[A-Za-z]:(?![/\\])", text):
            external.add("UNC/device/drive-relative reference (review exact command)")
    for value in values:
        path = PureWindowsPath(value)
        if not path.is_relative_to(project) or ".." in path.parts:
            external.add(str(path))
    return tuple(sorted(external))


def permission_scope(cwd: str, references: tuple[str, ...]) -> str:
    # Exact request scope, not a drive-wide wildcard; normalize Windows case.
    return json.dumps(
        {
            "cwd": str(PureWindowsPath(cwd)).casefold(),
            "references": [p.casefold() for p in references],
        },
        sort_keys=True,
    )
