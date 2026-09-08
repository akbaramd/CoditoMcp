"""Actionable Windows toasts with one-use, daemon-bound activation tokens."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

APP_ID = "Codito.DeviceMcp"
SCHEME = "codito-approval"


def register_activation() -> bool:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    import winreg

    # Per-user app registration only. Never enable/override Windows notification settings.
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}"
    ) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Codito approvals")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Codito local approval")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}\shell\open\command"
    ) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{sys.executable}" --toast-activation "%1"')
    return True


def activation_request(uri: str) -> dict[str, str]:
    if len(uri) > 2048:
        raise ValueError("Invalid activation size")
    parsed = urlsplit(uri)
    if (
        parsed.scheme != SCHEME
        or parsed.netloc != "decision"
        or parsed.path not in {"", "/"}
        or parsed.fragment
    ):
        raise ValueError("Invalid activation route")
    values = parse_qs(parsed.query, strict_parsing=True)
    if set(values) != {"request_id", "decision", "token"} or any(
        len(v) != 1 for v in values.values()
    ):
        raise ValueError("Invalid activation fields")
    result = {key: value[0] for key, value in values.items()}
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in result.values()):
        raise ValueError("Invalid activation value")
    if result["decision"] not in {
        "deny",
        "allow_once",
        "allow_always_read",
        "allow_always_shell",
        "review",
    }:
        raise ValueError("Invalid activation decision")
    return {"action": "approval.toast", **result}


def toast_specification(approval: dict[str, Any]) -> dict[str, Any]:
    tokens = approval["toast_tokens"]

    def uri(decision: str) -> str:
        return f"{SCHEME}://decision?" + urlencode(
            {
                "request_id": approval["request_id"],
                "decision": decision,
                "token": tokens[decision],
            }
        )

    root = ET.Element(
        "toast", {"activationType": "protocol", "launch": uri("review"), "duration": "long"}
    )
    binding = ET.SubElement(ET.SubElement(root, "visual"), "binding", {"template": "ToastGeneric"})
    ET.SubElement(binding, "text").text = "Codito approval required"
    ET.SubElement(binding, "text").text = str(approval.get("project_title", ""))[:120]
    ET.SubElement(binding, "text").text = (
        str(approval.get("summary", ""))[:350]
        + "\nScope: "
        + str(approval.get("working_directory") or approval.get("requested_external_paths", ""))[
            :250
        ]
    )
    actions = ET.SubElement(root, "actions")
    always = (
        "allow_always_read"
        if approval.get("persistent_read_eligible")
        else "allow_always_shell"
        if approval.get("persistent_shell_eligible")
        else "review"
    )
    for label, decision in (
        ("Deny", "deny"),
        ("Allow", "allow_once"),
        ("Always allow" if always != "review" else "Review details", always),
    ):
        ET.SubElement(
            actions,
            "action",
            {"content": label, "activationType": "protocol", "arguments": uri(decision)},
        )
    return {
        "xml": ET.tostring(root, encoding="unicode"),
        "tag": hashlib.sha256(approval["request_id"].encode()).hexdigest()[:16],
        # System.Text.Json DateTimeOffset requires the ISO 'T', not Python str(datetime).
        "expires_at": datetime.fromisoformat(str(approval["deadline_at"])).isoformat(),
    }


def show_native_toast(broker: Path | None, approval: dict[str, Any]) -> None:
    if broker is None:
        raise ValueError("Notification broker missing")
    payload = {"version": 1, "operation": "notify", "toast": toast_specification(approval)}
    result = subprocess.run(  # noqa: S603 - fixed installed broker; data only travels on stdin.
        [str(broker), "--json"],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=5,
        creationflags=0x08000000,
        check=False,
    )
    if result.returncode != 0 or json.loads(result.stdout).get("ok") is not True:
        raise ValueError("Windows rejected notification")
