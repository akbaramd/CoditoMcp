"""Synthetic Qt/IPC tests only; never request a real screen or Windows permission."""

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QSystemTrayIcon

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
            response = responses.get(value["action"], {"ok": True, "projects": [], "online": True})
            return response(value) if callable(response) else response

    monkeypatch.setattr(CoditoMainWindow, "_check_automatic_update", lambda _: None)
    # Never repair a real registry association from a synthetic UI test.
    monkeypatch.setattr("codito_agent.desktop_ui.register_activation", lambda: False)
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


def test_second_request_gets_notified_behind_pending_head_without_repeat_storm(
    background_window, monkeypatch
):
    window, calls, responses = background_window
    head = {**pending_approval(), "request_id": "pending_diagnostic"}
    second = {**pending_approval(), "request_id": "pending_screenshot"}
    live = [head, second]

    def next_pending(value):
        selected = value.get("request_id")
        return {
            "ok": True,
            "pending_ids": [item["request_id"] for item in live],
            "approval": next(
                (
                    item
                    for item in live
                    if (
                        item["request_id"] == selected
                        if selected
                        else item["request_id"] not in value.get("exclude_ids", [])
                    )
                ),
                None,
            ),
        }

    responses["approval.next"] = next_pending
    notified = []
    monkeypatch.setattr(window, "_notify_approval", notified.append)
    monkeypatch.setattr(window, "_show_approval", lambda _: pytest.fail("Automatic modal"))
    monkeypatch.setattr(window, "showNormal", lambda: pytest.fail("Stole foreground"))
    for _ in range(5):
        window.poll_approval()
    assert notified == [head, second]
    assert window._notified_approvals == {"pending_diagnostic", "pending_screenshot"}
    assert any(
        call == {"action": "approval.next", "exclude_ids": ["pending_diagnostic"]} for call in calls
    )
    # A resolved head is pruned, but the still-pending second request is not re-notified.
    live.remove(head)
    window.poll_approval()
    assert window._notified_approvals == {"pending_screenshot"}
    assert notified == [head, second]
    reviewed = []
    monkeypatch.setattr(window, "_show_approval", reviewed.append)
    window.review_pending_approvals(request_id="pending_screenshot")
    assert reviewed == [second]
    assert calls[-1] == {"action": "approval.next", "request_id": "pending_screenshot"}
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


@pytest.fixture
def corner_window(background_window, monkeypatch):
    from codito_agent import desktop_capture

    window, calls, responses = background_window
    monkeypatch.setattr(desktop_capture, "desktop_is_unlocked", lambda: True)
    monkeypatch.setattr(window, "showNormal", lambda: pytest.fail("Stole foreground"))
    monkeypatch.setattr(window, "raise_", lambda: pytest.fail("Raised main window"))
    monkeypatch.setattr(window, "activateWindow", lambda: pytest.fail("Activated main window"))

    class TestScreen:
        @staticmethod
        def availableGeometry():
            return QRect(0, 0, 1920, 2160)

    monkeypatch.setattr(QApplication, "primaryScreen", TestScreen)
    live = ["synthetic_only"]
    responses["approval.next"] = lambda _: {"ok": True, "pending_ids": list(live)}
    approval = {
        **pending_approval(),
        "toast_tokens": {  # Inert strings: FakeClient never contacts the daemon.
            "deny": "test-deny",
            "allow_once": "test-once",
            "allow_always_screen": "test-monitor-only",
        },
    }
    return window, calls, responses, approval, live


def test_local_panel_has_nonactivating_buttons_and_only_marks_real_display(corner_window):
    window, calls, _, approval, _ = corner_window
    window._notify_approval(approval)
    panel = window._corner_panels[approval["request_id"]]
    assert panel.isVisible()
    assert not window.isVisible()
    assert panel.windowModality() == Qt.WindowModality.NonModal
    assert panel.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    buttons = panel.findChildren(QPushButton)
    assert [button.text() for button in buttons] == ["Deny", "Allow", "Always allow", "Details"]
    assert all(button.focusPolicy() == Qt.FocusPolicy.NoFocus for button in buttons)
    assert all(not button.isDefault() for button in buttons)
    assert {"action": "approval.displayed", "request_id": "synthetic_only"} in calls
    assert not any(item["action"] in {"approval.toast", "approval.respond"} for item in calls)


@pytest.mark.parametrize("user_state", ["Busy", "AcceptsNotifications"])
def test_native_submission_is_not_display_proof_and_fallback_remains(
    corner_window, monkeypatch, user_state
):
    from codito_agent import desktop_ui
    from codito_agent.notifications import ToastDelivery

    window, calls, _, approval, _ = corner_window
    clock = [100.0]
    monkeypatch.setattr(desktop_ui.time, "monotonic", lambda: clock[0])
    workers = []
    monkeypatch.setattr(
        desktop_ui.ApprovalToastWorker, "start", lambda worker: workers.append(worker)
    )
    window._actionable_toasts = True
    window._notify_approval(approval)
    worker = workers[0]
    assert not any(item["action"] == "approval.displayed" for item in calls)
    worker.delivered.emit(ToastDelivery("Enabled", user_state))
    if user_state == "AcceptsNotifications":
        assert not window._corner_panels
        assert not any(item["action"] == "approval.displayed" for item in calls)
        clock[0] += 3.1
        window._refresh_corner_panels()
    assert window._corner_panels[approval["request_id"]].isVisible()
    worker.finished.emit()


def test_native_submission_failure_shows_local_panel(corner_window, monkeypatch):
    from codito_agent import desktop_ui

    window, _, _, approval, _ = corner_window
    workers = []
    monkeypatch.setattr(
        desktop_ui.ApprovalToastWorker, "start", lambda worker: workers.append(worker)
    )
    window._actionable_toasts = True
    window._notify_approval(approval)
    workers[0].failed.emit()
    assert window._corner_panels[approval["request_id"]].isVisible()
    workers[0].finished.emit()


def test_corner_buttons_hide_then_dismiss_then_use_only_bound_token(corner_window, monkeypatch):
    from codito_agent import desktop_ui

    window, calls, _, approval, _ = corner_window
    workers = []
    callbacks = []
    monkeypatch.setattr(
        desktop_ui.ApprovalDismissWorker, "start", lambda worker: workers.append(worker)
    )
    monkeypatch.setattr(
        desktop_ui.QTimer, "singleShot", lambda _, callback: callbacks.append(callback)
    )
    window._notify_approval(approval)
    panel = window._corner_panels[approval["request_id"]]
    next(
        button for button in panel.findChildren(QPushButton) if button.text() == "Always allow"
    ).click()
    assert not panel.isVisible()
    assert not any(item["action"] == "approval.toast" for item in calls)
    window._refresh_corner_panels()
    assert not any(item.isVisible() for item in window._corner_panels.values())
    workers[0].finished.emit()
    assert not any(item["action"] == "approval.toast" for item in calls)
    callbacks[0]()
    decisions = [item for item in calls if item["action"] == "approval.toast"]
    assert decisions == [
        {
            "action": "approval.toast",
            "request_id": "synthetic_only",
            "decision": "allow_always_screen",
            "token": "test-monitor-only",
        }
    ]
    window._corner_decision("synthetic_only", "allow_always_screen")
    assert len([item for item in calls if item["action"] == "approval.toast"]) == 1


@pytest.mark.parametrize("state", ["locked", "expired"])
def test_delayed_button_never_sends_on_lock_or_expiry(corner_window, monkeypatch, state):
    from codito_agent import desktop_capture, desktop_ui

    window, calls, _, approval, _ = corner_window
    workers = []
    callbacks = []
    monkeypatch.setattr(
        desktop_ui.ApprovalDismissWorker, "start", lambda worker: workers.append(worker)
    )
    monkeypatch.setattr(
        desktop_ui.QTimer, "singleShot", lambda _, callback: callbacks.append(callback)
    )
    window._notify_approval(approval)
    window._corner_decision("synthetic_only", "allow_once")
    workers[0].finished.emit()
    if state == "locked":
        monkeypatch.setattr(desktop_capture, "desktop_is_unlocked", lambda: False)
    else:
        approval["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    callbacks[0]()
    assert not any(item["action"] in {"approval.toast", "approval.respond"} for item in calls)


def test_locked_desktop_never_shows_fallback_and_lock_hides_existing(corner_window, monkeypatch):
    from codito_agent import desktop_capture

    window, calls, _, approval, _ = corner_window
    monkeypatch.setattr(desktop_capture, "desktop_is_unlocked", lambda: False)
    window._notify_approval(approval)
    assert not window._corner_panels
    assert not any(item["action"] == "approval.displayed" for item in calls)
    monkeypatch.setattr(desktop_capture, "desktop_is_unlocked", lambda: True)
    window._refresh_corner_panels()
    assert window._corner_panels["synthetic_only"].isVisible()
    monkeypatch.setattr(desktop_capture, "desktop_is_unlocked", lambda: False)
    window._refresh_corner_panels()
    assert not window._corner_panels["synthetic_only"].isVisible()


@pytest.mark.parametrize("resolution", ["expired", "answered", "disconnected"])
def test_stale_or_unverifiable_panels_have_no_action(corner_window, resolution):
    window, calls, responses, approval, live = corner_window
    window._notify_approval(approval)
    panel = window._corner_panels["synthetic_only"]
    if resolution == "expired":
        approval["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif resolution == "answered":
        live.clear()
    else:
        responses["approval.next"] = {"ok": False}
    window._refresh_corner_panels()
    assert not panel.isVisible()
    assert not any(item["action"] in {"approval.toast", "approval.respond"} for item in calls)


def test_corner_queue_keeps_fourth_pending_until_slot_available(corner_window):
    window, _, _, approval, live = corner_window
    live[:] = [f"synthetic_{number}" for number in range(4)]
    for identifier in live:
        window._notify_approval({**approval, "request_id": identifier})
    assert len(window._corner_pending) == 4
    assert sum(panel.isVisible() for panel in window._corner_panels.values()) == 3
    live.remove("synthetic_0")
    window._refresh_corner_panels()
    assert "synthetic_0" not in window._corner_pending
    assert window._corner_panels["synthetic_3"].isVisible()


def test_details_is_explicit_exact_review_without_decision(corner_window, monkeypatch):
    window, calls, _, approval, _ = corner_window
    reviewed = []
    monkeypatch.setattr(window, "review_pending_approvals", lambda **kw: reviewed.append(kw))
    window._notify_approval(approval)
    panel = window._corner_panels["synthetic_only"]
    assert not reviewed
    next(button for button in panel.findChildren(QPushButton) if button.text() == "Details").click()
    assert reviewed == [{"request_id": "synthetic_only"}]
    assert not panel.isVisible()
    assert not any(item["action"] in {"approval.toast", "approval.respond"} for item in calls)


def test_already_authorized_capture_hides_panels_and_completes_claim_once(
    corner_window, monkeypatch
):
    from codito_agent import desktop_capture, desktop_ui

    window, calls, responses, approval, _ = corner_window
    window._notify_approval(approval)
    panel = window._corner_panels["synthetic_only"]
    captures = []
    callbacks = []
    monkeypatch.setattr(
        desktop_ui.QTimer, "singleShot", lambda _, callback: callbacks.append(callback)
    )
    monkeypatch.setattr(
        desktop_capture,
        "capture_screen",
        lambda *args: captures.append((args, panel.isVisible())) or {"synthetic": True},
    )
    responses["screen.next"] = {
        "capture": {
            "capture_id": "synthetic_capture",
            "action": "capture",
            "display_id": "test_monitor",
            "max_dimension": 100,
            "topology_id": "test_layout",
            "deadline_at": approval["deadline_at"],
        }
    }
    window.poll_capture()
    assert not panel.isVisible()
    assert not captures
    window._refresh_corner_panels()
    window.poll_capture()
    assert len([item for item in calls if item["action"] == "screen.next"]) == 1
    callbacks[0]()
    assert captures == [(("test_monitor", 100, "test_layout"), False)]
    assert len([item for item in calls if item["action"] == "screen.respond"]) == 1


@pytest.mark.parametrize("kind", ["show", "dismiss"])
def test_quit_retains_running_notification_thread_until_finished(corner_window, monkeypatch, kind):
    from codito_agent import desktop_ui

    window, calls, _, approval, _ = corner_window
    entered = threading.Event()
    release = threading.Event()
    quit_calls = []
    monkeypatch.setattr(QApplication, "quit", lambda: quit_calls.append(True))

    def blocked_run(_):
        entered.set()
        release.wait(3)

    worker_type = (
        desktop_ui.ApprovalToastWorker if kind == "show" else desktop_ui.ApprovalDismissWorker
    )
    monkeypatch.setattr(worker_type, "run", blocked_run)
    window._actionable_toasts = kind == "show"
    window._notify_approval(approval)
    if kind == "dismiss":
        window._corner_decision("synthetic_only", "allow_once")
    worker = window._toast_workers[0]
    try:
        assert entered.wait(1)
        assert worker.isRunning()
        window.refresh_timer.start(2500)
        window.quit_ui()
        assert not quit_calls
        assert window._toast_workers == [worker]
        assert not window.refresh_timer.isActive()
        assert not window._corner_pending
        assert not window.isVisible()
        close_event = QCloseEvent()
        window.closeEvent(close_event)
        assert not close_event.isAccepted()
        if kind == "show":
            worker.failed.emit()
        window.poll_approval()
        window._notify_approval(approval)
        assert not window._corner_pending
        assert len(window._toast_workers) == 1
    finally:
        release.set()
        assert worker.wait(3000)
        QApplication.processEvents()
    assert quit_calls == [True]
    assert not window._toast_workers
    assert not window._corner_deciding
    assert not any(item["action"] in {"approval.toast", "approval.respond"} for item in calls)


def test_quit_drops_pending_auto_install_and_account_work(background_window, monkeypatch):
    from codito_agent import desktop_ui
    from codito_agent.updates import ReleaseUpdate

    window, _, _ = background_window
    quit_calls = []
    monkeypatch.setattr(QApplication, "quit", lambda: quit_calls.append(True))
    monkeypatch.setattr(desktop_ui.UpdateWorker, "start", lambda _: pytest.fail("Started update"))
    monkeypatch.setattr(desktop_ui.AccountWorker, "start", lambda _: pytest.fail("Started login"))
    update = object.__new__(ReleaseUpdate)
    worker = desktop_ui.UpdateWorker()
    window._update_worker = worker
    window._pending_update = update
    window.quit_ui()
    assert not quit_calls
    assert window._pending_update is None
    window._update_finished()
    assert quit_calls == [True]
    window._start_update_install(update)
    window.check_for_updates(silent=True)
    window._start_account_worker("login")


def test_update_handoff_uses_worker_safe_quit(background_window, monkeypatch):
    window, _, _ = background_window
    shutdown = []
    monkeypatch.setattr(window, "quit_ui", lambda: shutdown.append(True))
    window._update_install_started("0.0.0-test")
    assert shutdown == [True]


def test_quit_completes_deferred_capture_without_collecting_pixels(corner_window, monkeypatch):
    from codito_agent import desktop_capture, desktop_ui

    window, calls, responses, approval, _ = corner_window
    window._notify_approval(approval)
    callbacks = []
    quit_calls = []
    monkeypatch.setattr(QApplication, "quit", lambda: quit_calls.append(True))
    monkeypatch.setattr(
        desktop_ui.QTimer, "singleShot", lambda _, callback: callbacks.append(callback)
    )
    monkeypatch.setattr(
        desktop_capture, "capture_screen", lambda *_: pytest.fail("Pixels after quit")
    )
    responses["screen.next"] = {
        "capture": {
            "capture_id": "test_shutdown",
            "action": "capture",
            "deadline_at": approval["deadline_at"],
        }
    }
    window.poll_capture()
    assert window._capture_in_progress
    window.quit_ui()
    assert not quit_calls
    callbacks[0]()
    assert not window._capture_in_progress
    assert quit_calls == [True]
    assert {
        "action": "screen.respond",
        "capture_id": "test_shutdown",
        "result": {"ok": False},
    } in calls
