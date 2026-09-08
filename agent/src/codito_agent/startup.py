from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import AgentError

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DAEMON_VALUE_NAME = "Codito Agent Daemon"
UI_VALUE_NAME = "Codito Agent Tray"


def enable_startup(
    executable: Path,
    daemon_arguments: list[str],
    ui_arguments: list[str],
) -> None:
    if os.name != "nt":
        raise AgentError("unsupported_platform", "Startup registration requires Windows")
    import winreg

    daemon_command = subprocess.list2cmdline([str(executable), *daemon_arguments])
    ui_command = subprocess.list2cmdline([str(executable), *ui_arguments])
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, access=winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, DAEMON_VALUE_NAME, 0, winreg.REG_SZ, daemon_command)
        winreg.SetValueEx(key, UI_VALUE_NAME, 0, winreg.REG_SZ, ui_command)


def disable_startup() -> None:
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, access=winreg.KEY_SET_VALUE) as key:
            for value_name in (DAEMON_VALUE_NAME, UI_VALUE_NAME, "Codito Agent"):
                try:
                    winreg.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass
