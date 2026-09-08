from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from codito_agent.config import AgentConfig
from codito_agent.desktop_ui import CoditoMainWindow


class FakeIpcClient:
    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["action"] == "activity.list":
            return {"ok": True, "activity": []}
        return {
            "ok": True,
            "online": True,
            "device_name": "Windows workstation",
            "device_id": "5793fd41-7b9f-4d11-9522-3acda63f8f12",
            "account_id": "account-12345678",
            "mcp_url": "https://codito.example/mcp/d/link-123",
            "connection_epoch": 7,
            "pending_approvals": 0,
            "projects": [
                {
                    "project_id": "project-123",
                    "title": "CoditoMcp",
                    "root": r"D:\Workspaces\CoditoMcp",
                    "mode": "isolated",
                    "enabled": True,
                }
            ],
        }


def test_desktop_dashboard_renders_live_device_state(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication(["Codito UI test"])
    config = AgentConfig(
        relay_http_url="https://codito.example",
        data_directory=tmp_path,
    )
    tray = QSystemTrayIcon()
    window = CoditoMainWindow(config, FakeIpcClient(), tray)  # type: ignore[arg-type]
    window.refresh_timer.stop()
    window.approval_timer.stop()
    window.project_request_timer.stop()

    assert window.status_pill.text() == "●  ONLINE"
    assert window.mcp_url.text() == "https://codito.example/mcp/d/link-123"
    assert window.projects_table.rowCount() == 1
    assert window.projects_table.item(0, 0).text() == "CoditoMcp"

    window._show_page(1)
    assert window.header_title.text() == "Projects"
    assert window.nav_group.checkedId() == 1

    window._quitting = True
    window.close()
    tray.hide()
    app.processEvents()
