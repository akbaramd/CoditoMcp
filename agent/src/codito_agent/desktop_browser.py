"""Open a locally approved HTTP URL; never run arbitrary GUI commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from codito_protocol.desktop_action import BrowserChoice, DeviceDesktopInput
from pydantic import ValidationError


def _desktop_unlocked() -> bool:
    from .desktop_capture import desktop_is_unlocked

    return desktop_is_unlocked()


def _open_default(url: str) -> bool:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    return bool(QDesktopServices.openUrl(QUrl(url)))


def _firefox_executable() -> Path | None:
    """Use only the installed Firefox registration, never cwd or PATH lookup."""
    if os.name != "nt":
        return None
    import winreg

    key_name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    raw, kind = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(raw, str):
                continue
            if kind == winreg.REG_EXPAND_SZ:
                raw = os.path.expandvars(raw)
            # App Paths contains an executable, not an executable plus arguments.
            path = Path(raw.strip().strip('"'))
            if (
                not path.is_absolute()
                or path.drive.startswith("\\")
                or path.name.casefold() != "firefox.exe"
                or not path.is_file()
            ):
                continue
            return path
    return None


def _browser_environment() -> dict[str, str]:
    """Do not give a browser the agent's credentials or Python/packager variables."""
    standard = {
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "COMMONPROGRAMFILES",
        "TEMP",
        "TMP",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "USERDOMAIN",
    }
    environment = {name: value for name, value in os.environ.items() if name.upper() in standard}
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    environment["PATH"] = str(Path(system_root) / "System32")
    return environment


def open_browser_url(browser: BrowserChoice, url: str) -> bool:
    """Return OS submission acceptance only; this cannot verify page load."""
    try:
        request = DeviceDesktopInput(
            browser=browser, url=url, purpose="Approved local browser request"
        )
    except ValidationError:
        return False
    if not _desktop_unlocked():
        return False
    if request.browser == "default":
        return _open_default(request.url)
    executable = _firefox_executable()
    if executable is None:
        return False
    try:
        subprocess.Popen(  # noqa: S603 - fixed registered executable and validated HTTP URL only.
            [str(executable), "-new-tab", request.url],
            executable=str(executable),
            cwd=str(executable.parent),
            env=_browser_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            shell=False,
        )
    except OSError:
        return False
    return True
