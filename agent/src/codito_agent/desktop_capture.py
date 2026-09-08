"""Interactive desktop capture; never a service or secure-desktop capture."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from datetime import UTC, datetime
from typing import Any

from codito_protocol.screenshot import DisplayCatalog
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication

MAX_DISPLAYS = 16
PIXEL_ROUNDING_TOLERANCE = 2


class _DisplayDevice(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    )


class _MonitorInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    )


def _windows_display_name(screen: Any) -> str:
    name = str(screen.name())
    if os.name != "nt" or name.startswith("\\\\.\\DISPLAY"):
        return name
    # Recent Qt returns EDID-friendly names. Map its Windows-native screen origin
    # back to the HMONITOR, checking full pixel geometry to refuse ambiguous maps.
    geometry = screen.geometry()
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user.MonitorFromPoint.restype = wintypes.HANDLE
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    user.GetMonitorInfoW.restype = wintypes.BOOL
    monitor = user.MonitorFromPoint(wintypes.POINT(geometry.x(), geometry.y()), 0)
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if not monitor or not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return ""
    rect = info.rcMonitor
    if (
        rect.left != geometry.x()
        or rect.top != geometry.y()
        or abs(rect.right - rect.left - round(geometry.width() * screen.devicePixelRatio()))
        > PIXEL_ROUNDING_TOLERANCE
        or abs(rect.bottom - rect.top - round(geometry.height() * screen.devicePixelRatio()))
        > PIXEL_ROUNDING_TOLERANCE
    ):
        return ""
    return str(info.szDevice)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _monitor_interface(display_name: str) -> str:
    """Identify one monitor interface, never a mutable DISPLAY1 alias alone."""
    if os.name != "nt" or not display_name:
        return ""
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.EnumDisplayDevicesW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(_DisplayDevice),
        wintypes.DWORD,
    ]
    user.EnumDisplayDevicesW.restype = wintypes.BOOL
    identifiers = []
    for index in range(MAX_DISPLAYS):
        monitor = _DisplayDevice()
        monitor.cb = ctypes.sizeof(monitor)
        if not user.EnumDisplayDevicesW(display_name, index, ctypes.byref(monitor), 1):
            break
        if monitor.StateFlags & 1 and monitor.DeviceID:
            identifiers.append(str(monitor.DeviceID).casefold())
    return identifiers[0] if len(identifiers) == 1 else ""


def _edid_identity(interface: str) -> str:
    """Return a validated monitor-configuration fingerprint, not a raw device path."""
    if os.name != "nt" or not interface:
        return ""
    import winreg

    parts = interface.split("#")
    if len(parts) != 4 or not parts[0].endswith("display"):
        return ""
    if any("\\" in part or "/" in part for part in parts[1:3]):
        return ""
    path = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[1]}\{parts[2]}\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ) as key:
            data, kind = winreg.QueryValueEx(key, "EDID")
    except OSError:
        return ""
    if kind != winreg.REG_BINARY or not isinstance(data, bytes) or len(data) < 128:
        return ""
    if data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00" or sum(data[:128]) % 256:
        return ""
    return hashlib.sha256(data[:128]).hexdigest()


def _catalog_screens() -> tuple[dict[str, Any], dict[str, Any]]:
    if not desktop_is_unlocked():
        raise ValueError("Desktop unavailable")
    screens = QGuiApplication.screens()
    primary = QGuiApplication.primaryScreen()
    if not screens or len(screens) > MAX_DISPLAYS or primary is None:
        raise ValueError("Display catalog unavailable")
    displays: list[dict[str, Any]] = []
    objects: dict[str, Any] = {}
    topology: list[dict[str, Any]] = []
    for index, screen in enumerate(screens):
        geometry = screen.geometry()
        interface = _monitor_interface(_windows_display_name(screen))
        serial = screen.serialNumber().strip()
        edid = _edid_identity(interface)
        # Consent is to this display configuration: unique Windows interface plus
        # serial or validated EDID. Identical serial-less hardware at the same port
        # can keep that logical identity; it is not a forensic physical identity.
        persistent = bool(interface and (serial or edid))
        physical = [interface, screen.manufacturer(), screen.model(), serial, edid]
        identity = _digest(physical if persistent else [*physical, screen.name()])
        identifier = "screen_" + identity
        if identifier in objects:
            raise ValueError("Ambiguous display identity")
        label = f"Screen {index + 1}: {screen.manufacturer()} {screen.model()}".strip()[:160]
        descriptor = {
            "id": identifier,
            "label": label,
            "primary": screen == primary,
            "width": geometry.width(),
            "height": geometry.height(),
            "scale_factor": screen.devicePixelRatio(),
            "identity": identity,
            "persistent_permission_supported": persistent,
        }
        displays.append(descriptor)
        objects[identifier] = screen
        topology.append({**descriptor, "x": geometry.x(), "y": geometry.y()})
    catalog = {
        "displays": displays,
        "topology_id": _digest(sorted(topology, key=lambda x: x["id"])),
    }
    return DisplayCatalog.model_validate(catalog).model_dump(mode="json"), objects


def list_displays() -> dict[str, Any]:
    """Metadata only. This function never calls grabWindow or captures pixels."""
    return _catalog_screens()[0]


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


def capture_screen(display_id: str, max_dimension: int, topology_id: str) -> dict[str, Any]:
    if not 640 <= max_dimension <= 2048 or not desktop_is_unlocked():
        raise ValueError("Desktop unavailable")
    catalog, screens = _catalog_screens()
    if catalog["topology_id"] != topology_id or display_id not in screens:
        raise ValueError("Display or layout changed; request fresh approval")
    screen = screens[display_id]
    image = screen.grabWindow(0).toImage()
    if image.isNull():
        raise ValueError("No desktop image")
    if not desktop_is_unlocked() or list_displays()["topology_id"] != topology_id:
        raise ValueError("Desktop changed while capturing; discard pixels")
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
            if not desktop_is_unlocked() or list_displays()["topology_id"] != topology_id:
                raise ValueError("Desktop changed while encoding; discard pixels")
            return {
                "mime_type": "image/png",
                "display": display_id,
                "width": scaled.width(),
                "height": scaled.height(),
                "captured_at": datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "image_base64": base64.b64encode(data).decode("ascii"),
            }
        dimension = int(dimension * 0.75)
    raise ValueError("Screenshot exceeds image size bound")
