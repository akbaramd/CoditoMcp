"""Actionable Windows toasts with one-use, daemon-bound activation tokens."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .diagnostics import event

APP_ID = "Codito.DeviceMcp"
SCHEME = "codito-approval"
PROG_ID = "Codito.ApprovalUrl"
CAPABILITIES = r"Software\Codito\Capabilities"
TOAST_ACTIVATOR_CLSID = "{0D673CF7-8613-4C9C-8D35-8A3F66BCE1A9}"
_listener: subprocess.Popen[bytes] | None = None
_listener_lock = threading.Lock()


def stop_activation_listener() -> None:
    global _listener
    with _listener_lock:
        process, _listener = _listener, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=3)
        if process.stdout is not None:
            process.stdout.close()


atexit.register(stop_activation_listener)


def ensure_activation_listener(broker: Path) -> None:
    """Keep an already-registered COM class alive for the tray's lifetime only."""
    global _listener
    with _listener_lock:
        if _listener is not None and _listener.poll() is None:
            return
        process = subprocess.Popen(  # noqa: S603 - fixed installed broker, no shell or user args.
            [str(broker), "--toast-listener"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        assert process.stdout is not None
        ready = threading.Event()
        reply: list[bytes] = []

        def read_ready() -> None:
            assert process.stdout is not None
            try:
                reply.append(process.stdout.readline(32))
            except OSError:
                pass
            finally:
                ready.set()

        thread = threading.Thread(target=read_ready, daemon=True)
        thread.start()
        if not ready.wait(4) or reply != [b"ready\r\n"]:
            process.terminate()
            process.wait(timeout=3)
            thread.join(timeout=1)
            if process.stdin is not None:
                process.stdin.close()
            process.stdout.close()
            event("toast_activation_listener_failed", detail="startup_not_ready")
            raise ValueError("Notification activation listener unavailable")
        _listener = process
        event("toast_activation_listener_ready")


def _association_command() -> str:
    """Ask the Shell about the effective handler, including the user's default."""
    import ctypes
    from ctypes import wintypes

    query = ctypes.WinDLL("shlwapi").AssocQueryStringW
    query.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = ctypes.c_long
    output = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(output))
    # ASSOCF_IS_PROTOCOL, ASSOCSTR_COMMAND. Registry presence alone is insufficient.
    result = query(0x1000, 1, SCHEME, "open", output, ctypes.byref(length))
    if result != 0:
        raise OSError("notification_protocol_unresolved")
    return output.value


def _notify_association_changed() -> None:
    import ctypes
    from ctypes import wintypes

    notify = ctypes.WinDLL("shell32").SHChangeNotify
    notify.argtypes = [wintypes.LONG, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
    notify.restype = None
    # SHCNE_ASSOCCHANGED + SHCNF_IDLIST | SHCNF_FLUSH: publish to the running Shell.
    notify(0x08000000, 0x1000, None, None)


def activation_registered(executable: Path) -> bool:
    import winreg

    broker = executable.parent.parent / "broker" / "Codito.Broker.exe"
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}"
    ) as key:
        clsid, _ = winreg.QueryValueEx(key, "CustomActivator")
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        rf"Software\Classes\CLSID\{TOAST_ACTIVATOR_CLSID}\LocalServer32",
    ) as key:
        command, _ = winreg.QueryValueEx(key, "")
    return bool(
        clsid == TOAST_ACTIVATOR_CLSID
        and command.casefold() == f'"{broker}" --toast-server'.casefold()
        and broker.is_file()
    )


def register_activation(executable: Path | None = None) -> bool:
    if os.name != "nt" or (executable is None and not getattr(sys, "frozen", False)):
        return False
    import winreg

    executable = (executable or Path(sys.executable)).resolve(strict=True)
    if executable.name.lower() != "codito-agent-tray.exe":
        raise ValueError("Activation requires the installed Codito tray executable")
    command = f'"{executable}" --toast-activation "%1"'
    broker = (executable.parent.parent / "broker" / "Codito.Broker.exe").resolve(strict=True)
    icon = f'"{executable}",0'
    app_path = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable.name}"
    values = [
        (rf"Software\Classes\AppUserModelId\{APP_ID}", "DisplayName", "Codito approvals"),
        (rf"Software\Classes\AppUserModelId\{APP_ID}", "CustomActivator", TOAST_ACTIVATOR_CLSID),
        (
            rf"Software\Classes\CLSID\{TOAST_ACTIVATOR_CLSID}\LocalServer32",
            "",
            f'"{broker}" --toast-server',
        ),
        (CAPABILITIES, "ApplicationName", "Codito approvals"),
        (CAPABILITIES, "ApplicationDescription", "Local Codito approval decisions"),
        (CAPABILITIES, "ApplicationIcon", icon),
        (CAPABILITIES + r"\UrlAssociations", SCHEME, PROG_ID),
        (r"Software\RegisteredApplications", "Codito approvals", CAPABILITIES),
        (app_path, "", str(executable)),
        (app_path, "SupportedProtocols", SCHEME),
    ]
    for identity in (SCHEME, PROG_ID):
        base = rf"Software\Classes\{identity}"
        values.extend(
            [
                (base, "", "URL:Codito local approval"),
                (base, "URL Protocol", ""),
                (base + r"\Application", "ApplicationName", "Codito approvals"),
                (base + r"\DefaultIcon", "", icon),
                (base + r"\shell", "", "open"),
                (base + r"\shell\open\command", "", command),
            ]
        )
    # These are application-owned registrations, not UserChoice/default assignments.
    # Windows notification settings and other apps' associations are never changed.
    for path, name, value in values:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, app_path) as key:
        winreg.SetValueEx(key, "UseUrl", 0, winreg.REG_DWORD, 1)
    _notify_association_changed()
    ready = activation_registered(executable)
    if ready:
        cleanup = subprocess.run(  # noqa: S603 - fixed installed broker operation.
            [str(broker), "--json"],
            input=b'{"version":1,"operation":"notify-cleanup"}',
            capture_output=True,
            timeout=5,
            creationflags=0x08000000,
            check=False,
        )
        if cleanup.returncode != 0:
            event("toast_stale_cleanup_failed", detail="broker_rejected")
    event("toast_registration", detail="com_registered" if ready else "handler_mismatch")
    return ready


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
        "allow_always_screen",
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

    # Foreground means COM activation, not opening the Codito window. The sealed URI
    # is argument data only; Windows never launches it through protocol association.
    root = ET.Element(
        "toast", {"activationType": "foreground", "launch": uri("review"), "duration": "long"}
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
        else "allow_always_screen"
        if approval.get("persistent_screen_eligible")
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
            {"content": label, "activationType": "foreground", "arguments": uri(decision)},
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
    ensure_activation_listener(broker)
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
        # Broker messages outside this allowlist may contain user data; never log them.
        reason = result.stderr.decode(errors="replace").strip()
        event(
            "toast_delivery_failed",
            detail=reason
            if reason in {"notification_protocol_unavailable", "notification_disabled"}
            else "broker_rejected",
        )
        raise ValueError("Windows rejected notification")
