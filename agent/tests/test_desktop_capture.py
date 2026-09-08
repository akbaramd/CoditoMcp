from types import SimpleNamespace

import pytest
from codito_protocol.screenshot import DeviceScreenshotResult

pytest.importorskip("PySide6")
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage

from codito_agent import desktop_capture


class SyntheticDisplay:
    def __init__(self, name, serial, x):
        self._name = name
        self._serial = serial
        self._x = x
        self.grabs = 0

    def geometry(self):
        return QRect(self._x, 0, 1920, 1080)

    def name(self):
        return self._name

    def serialNumber(self):
        return self._serial

    def manufacturer(self):
        return "Synthetic"

    def model(self):
        return "Test Display"

    def devicePixelRatio(self):
        return 1.0

    def grabWindow(self, _handle):
        self.grabs += 1
        image = QImage(200, 100, QImage.Format.Format_RGB32)
        image.fill(0xFF112233)
        return SimpleNamespace(toImage=lambda: image)


@pytest.fixture
def displays(monkeypatch):
    first = SyntheticDisplay("first", "a", 0)
    second = SyntheticDisplay("second", "b", 1920)
    app = SimpleNamespace(screens=lambda: [first, second], primaryScreen=lambda: first)
    monkeypatch.setattr(desktop_capture, "QGuiApplication", app)
    monkeypatch.setattr(desktop_capture, "desktop_is_unlocked", lambda: True)
    monkeypatch.setattr(desktop_capture, "_windows_display_name", lambda screen: screen.name())
    monkeypatch.setattr(desktop_capture, "_monitor_interface", lambda name: "test_" + name)
    monkeypatch.setattr(desktop_capture, "_edid_identity", lambda _name: "")
    return first, second, app


def test_catalog_has_no_pixels_and_selected_capture_is_bounded(displays):
    first, second, _app = displays
    catalog = desktop_capture.list_displays()
    assert first.grabs == second.grabs == 0
    selected = catalog["displays"][1]
    image = DeviceScreenshotResult.model_validate(
        desktop_capture.capture_screen(selected["id"], 640, catalog["topology_id"])
    )
    assert first.grabs == 0 and second.grabs == 1
    assert image.display == selected["id"]
    assert image.width <= 640 and image.height <= 640
    assert len(image.image_base64) <= 800000
    assert all(item["persistent_permission_supported"] for item in catalog["displays"])


@pytest.mark.parametrize("change", ["primary", "position", "serial", "remove"])
def test_topology_change_refuses_pixels_for_stale_consent(displays, change):
    first, second, app = displays
    original = desktop_capture.list_displays()
    if change == "primary":
        app.primaryScreen = lambda: second
    elif change == "position":
        second._x = 2000
    elif change == "serial":
        first._serial = "replacement"
    else:
        app.screens = lambda: [second]
        app.primaryScreen = lambda: second
    with pytest.raises(ValueError, match="changed"):
        desktop_capture.capture_screen(original["displays"][0]["id"], 1600, original["topology_id"])
    assert first.grabs == second.grabs == 0


def test_unknown_physical_serial_disables_persistence(displays):
    first, _second, _app = displays
    first._serial = ""
    assert not desktop_capture.list_displays()["displays"][0]["persistent_permission_supported"]


def test_serialless_validated_edid_supports_configuration_consent(displays, monkeypatch):
    first, _second, _app = displays
    first._serial = ""
    monkeypatch.setattr(desktop_capture, "_edid_identity", lambda _name: "e" * 64)
    assert desktop_capture.list_displays()["displays"][0]["persistent_permission_supported"]


def test_lock_or_secure_desktop_refuses_metadata_and_pixels(displays, monkeypatch):
    first, second, _app = displays
    catalog = desktop_capture.list_displays()
    monkeypatch.setattr(desktop_capture, "desktop_is_unlocked", lambda: False)
    with pytest.raises(ValueError, match="unavailable"):
        desktop_capture.list_displays()
    with pytest.raises(ValueError, match="unavailable"):
        desktop_capture.capture_screen(catalog["displays"][0]["id"], 1600, catalog["topology_id"])
    assert first.grabs == second.grabs == 0


def test_lock_after_grab_discards_pixels(displays, monkeypatch):
    first, _second, _app = displays
    catalog = desktop_capture.list_displays()
    monkeypatch.setattr(desktop_capture, "desktop_is_unlocked", lambda: first.grabs == 0)
    with pytest.raises(ValueError, match="discard pixels"):
        desktop_capture.capture_screen(catalog["displays"][0]["id"], 1600, catalog["topology_id"])
    assert first.grabs == 1
