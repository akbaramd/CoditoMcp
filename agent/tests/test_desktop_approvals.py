"""Offscreen UI checks: never send decisions to a real daemon."""

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from codito_agent.config import AgentConfig
from codito_agent.desktop_ui import CoditoMainWindow


@pytest.mark.parametrize("real_event_loop", [False, True])
def test_native_approval_dialog_can_render_without_exception(
    tmp_path, monkeypatch, real_event_loop
):
    app = QApplication.instance() or QApplication([])
    requests = []

    class FakeClient:
        def request(self, value):
            requests.append(value)
            return {"ok": True, "online": True, "projects": []}

    if not real_event_loop:
        monkeypatch.setattr(QMessageBox, "exec", lambda _: 0)
    else:
        original_exec = QMessageBox.exec

        def run_synthetic_dialog(dialog):
            def close_synthetic_dialog():
                assert dialog.isVisible()
                assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
                dialog.reject()

            closer = QTimer(dialog)
            closer.setSingleShot(True)
            closer.timeout.connect(close_synthetic_dialog)
            closer.start(100)
            began = time.monotonic()
            result = original_exec(dialog)
            assert time.monotonic() - began < 3
            return result

        monkeypatch.setattr(QMessageBox, "exec", run_synthetic_dialog)
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
    try:
        window._show_approval(
            {
                "request_id": "test_approval_only",
                "summary": "Synthetic unit test",
                "project_title": "Unit test",
                "capability": "shell:execute",
                "risk": "native_execution",
                "deadline_at": str(datetime.now(UTC) + timedelta(seconds=5)),
                "command": {"kind": "script", "shell": "powershell", "script": "Write-Output test"},
            }
        )
        assert {
            "action": "approval.respond",
            "request_id": "test_approval_only",
            "decision": "deny",
        } in requests
        if real_event_loop:
            assert {"action": "approval.displayed", "request_id": "test_approval_only"} in requests
    finally:
        window._quitting = True
        window.close()
        window.deleteLater()
