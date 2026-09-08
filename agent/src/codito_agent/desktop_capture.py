"""Interactive desktop capture; never a service or secure-desktop capture."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
from ctypes import wintypes
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication


def desktop_is_unlocked() -> bool:
    if os.name != "nt":
        return False
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user.GetUserObjectInformationW.restype = wintypes.BOOL
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.CloseDesktop.restype = wintypes.BOOL
    handle = user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS only
    if not handle:
        return False
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        return (
            bool(
                user.GetUserObjectInformationW(
                    handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
                )
            )
            and name.value.casefold() == "default"
        )
    finally:
        user.CloseDesktop(handle)


def capture_primary_screen(max_dimension: int) -> dict[str, Any]:
    if not 640 <= max_dimension <= 2048 or not desktop_is_unlocked():
        raise ValueError("Desktop unavailable")
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        raise ValueError("No display")
    image = screen.grabWindow(0).toImage()
    if image.isNull():
        raise ValueError("No desktop image")
    dimension = max_dimension
    while dimension >= 320:
        scaled = image.scaled(
            dimension,
            dimension,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        raw = QByteArray()
        buffer = QBuffer(raw)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        # PySide 6 runtime accepts str here; its wheel stub incorrectly advertises bytes.
        saved = scaled.save(buffer, "PNG")  # type: ignore[call-overload, unused-ignore]
        buffer.close()
        data = bytes(raw.data())
        if saved and len(data) <= 600000:
            return {
                "mime_type": "image/png",
                "display": "primary",
                "width": scaled.width(),
                "height": scaled.height(),
                "captured_at": datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "image_base64": base64.b64encode(data).decode("ascii"),
            }
        dimension = int(dimension * 0.75)
    raise ValueError("Screenshot exceeds image size bound")
