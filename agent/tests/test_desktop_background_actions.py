"""Synthetic Qt/IPC tests only; never request a real screen or Windows permission."""

import os
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from codito_agent.config import AgentConfig
from codito_agent.desktop_ui import CoditoMainWindow


@pytest.fixture
def background_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    calls = []
    responses = {}

    class FakeClient:
        def request(self, value):
            calls.append(value)
            return responses.get(value["action"], {"ok": True, "projects": [], "online": True})

    monkeypatch.setattr(CoditoMainWindow, "_check_automatic_update", lambda _: None)
    tray = QSystemTrayIcon(app)
    window = CoditoMainWindow(AgentConfig(data_directory=tmp_path), FakeClient(), tray)
    for timer in (
        window.refresh_timer,
        window.approval_timer,
        window.capture_timer,
        window.project_request_timer,
        window.update_timer,
    ):
        timer.stop()
    yield window, calls, responses
    window._quitting = True
    window.close()
    window.deleteLater()


def pending_approval():
    return {
        "request_id": "synthetic_only",
        "summary": "Capture selected test display",
        "project_title": "Test monitor",
        "capability": "screen:read",
        "risk": "read",
        "deadline_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        "persistent_screen_eligible": True,
    }


def test_arrival_only_notifies_once_never_raises_codito(background_window, monkeypatch):
    window, calls, responses = background_window
    approval = pending_approval()
    responses["approval.next"] = {"ok": True, "approval": approval}
    notified = []
    monkeypatch.setattr(window, "_notify_approval", notified.append)
    monkeypatch.setattr(window, "_show_approval", lambda _: pytest.fail("Automatic modal"))
    monkeypatch.setattr(window, "showNormal", lambda: pytest.fail("Stole foreground"))
    window.poll_approval()
    window.poll_approval()
    assert notified == [approval]
    assert not any(item["action"] == "approval.respond" for item in calls)


def test_manual_always_screen_minimizes_before_sending_decision(background_window, monkeypatch):
    window, calls, _ = background_window

    def choose_synthetic_button(dialog):
        next(
            button for button in dialog.buttons() if "Always allow this monitor" in button.text()
        ).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", choose_synthetic_button)
    monkeypatch.setattr(window, "showMinimized", lambda: calls.append({"action": "minimized"}))
    window._show_approval(pending_approval())
    decision = {
        "action": "approval.respond",
        "request_id": "synthetic_only",
        "decision": "allow_always_screen",
    }
    assert decision in calls
    assert calls.index({"action": "minimized"}) < calls.index(decision)


def test_toast_review_opens_exact_request_not_queue_head(background_window, monkeypatch):
    window, _, responses = background_window
    responses["approval.review.next"] = {"ok": True, "request_id": "second_request"}
    reviewed = []
    monkeypatch.setattr(
        window, "review_pending_approvals", lambda *, request_id: reviewed.append(request_id)
    )
    monkeypatch.setattr(window, "_notify_approval", lambda _: pytest.fail("Wrong request"))
    window.poll_approval()
    assert reviewed == ["second_request"]


def test_metadata_request_never_grabs_pixels(background_window, monkeypatch):
    from codito_agent import desktop_capture

    window, calls, responses = background_window
    responses["screen.next"] = {
        "capture": {
            "capture_id": "metadata_only",
            "action": "list_displays",
            "deadline_at": pending_approval()["deadline_at"],
        }
    }
    monkeypatch.setattr(desktop_capture, "list_displays", lambda: {"displays": []})
    monkeypatch.setattr(
        desktop_capture, "capture_screen", lambda *_: pytest.fail("Unexpected pixels")
    )
    window.poll_capture()
    assert {
        "action": "screen.respond",
        "capture_id": "metadata_only",
        "result": {"displays": []},
    } in calls


def test_approved_browser_queue_uses_exact_browser_and_url(background_window, monkeypatch):
    from codito_agent import desktop_browser

    window, calls, responses = background_window
    responses["desktop.next"] = {
        "desktop_action": {
            "desktop_action_id": "browser_only",
            "browser": "firefox",
            "url": "https://example.com/",
            "deadline_at": pending_approval()["deadline_at"],
        }
    }
    opened = []
    monkeypatch.setattr(
        desktop_browser,
        "open_browser_url",
        lambda browser, url: opened.append((browser, url)) or True,
    )
    window.poll_desktop_action()
    responses["desktop.next"] = {"desktop_action": None}
    window.poll_desktop_action()
    assert opened == [("firefox", "https://example.com/")]
    assert {
        "action": "desktop.respond",
        "desktop_action_id": "browser_only",
        "result": {"ok": True},
    } in calls
