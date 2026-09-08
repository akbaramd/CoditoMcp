from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .ipc import IpcSecretStore, NamedPipeClient, default_pipe_name


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="codito-agent-ui")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--show-status",
        action="store_true",
        help="Open the local connection and project status dialog after launch",
    )
    arguments = parser.parse_args(argv)
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import (
            QApplication,
            QFileDialog,
            QInputDialog,
            QMenu,
            QMessageBox,
            QStyle,
            QSystemTrayIcon,
        )
    except ImportError as exc:
        raise SystemExit("Install the 'ui' extra to run codito-agent-ui") from exc

    config = AgentConfig.load(arguments.config)
    key = IpcSecretStore(config.data_directory / "ipc-key.dpapi").load_or_create()
    client = NamedPipeClient(default_pipe_name(), key)
    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("Codito agent")
    menu = QMenu()

    def request(value: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return client.request(value)
        except Exception:
            tray.setToolTip("Codito agent — daemon unavailable")
            return None

    def show_status() -> None:
        value = request({"action": "status"})
        if not value:
            QMessageBox.warning(None, "Codito", "The Codito daemon is unavailable.")
            return
        projects = (
            "\n".join(
                f"• {project['title']} — {project['mode']}\n  {project['root']}"
                for project in value.get("projects", [])
            )
            or "No projects registered."
        )
        status = "Online" if value.get("online") else "Offline"
        QMessageBox.information(
            None,
            "Codito status",
            f"Relay: {status}\nMCP URL: {value.get('mcp_url') or 'not enrolled'}\n\n{projects}",
        )

    def add_project() -> None:
        directory = QFileDialog.getExistingDirectory(None, "Select a fixed local project directory")
        if not directory:
            return
        title, accepted = QInputDialog.getText(None, "Project title", "Title:")
        if not accepted:
            return
        value = request({"action": "project.register", "title": title, "path": directory})
        if not value or not value.get("ok"):
            QMessageBox.warning(None, "Codito", "The project could not be registered.")

    def poll_approval() -> None:
        value = request({"action": "approval.next"})
        approval = value.get("approval") if value else None
        if not isinstance(approval, dict):
            return
        tray.showMessage(
            "Codito approval required",
            f"{approval.get('project_title')}: {approval.get('summary')}",
            QSystemTrayIcon.MessageIcon.Warning,
            10_000,
        )
        dialog = QMessageBox()
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Codito local approval")
        dialog.setText(str(approval.get("summary", "Remote operation")))
        details = {
            "account": approval.get("account_id"),
            "link": approval.get("link_id"),
            "device": approval.get("device_id"),
            "project": approval.get("project_title"),
            "capability": approval.get("capability"),
            "risk": approval.get("risk"),
            "deadline_at": approval.get("deadline_at"),
            "working_directory": approval.get("working_directory"),
            "command": approval.get("command"),
            "patch": approval.get("patch"),
            "environment_differences": approval.get("environment_differences"),
            "requested_network": approval.get("requested_network"),
            "requested_external_paths": approval.get("requested_external_paths"),
            "action_digest": approval.get("action_digest"),
        }
        dialog.setDetailedText(json.dumps(details, indent=2, ensure_ascii=False))
        dialog.addButton("Deny", QMessageBox.ButtonRole.RejectRole)
        once = dialog.addButton("Allow once", QMessageBox.ButtonRole.AcceptRole)
        session = None
        if approval.get("session_eligible"):
            session = dialog.addButton(
                "Allow similar access for this session", QMessageBox.ButtonRole.YesRole
            )
        dialog.exec()
        clicked = dialog.clickedButton()
        decision = (
            "allow_session" if clicked is session else "allow_once" if clicked is once else "deny"
        )
        request(
            {
                "action": "approval.respond",
                "request_id": approval["request_id"],
                "decision": decision,
            }
        )

    status_action = QAction("Status and MCP link")
    status_action.triggered.connect(show_status)
    menu.addAction(status_action)
    add_action = QAction("Add project…")
    add_action.triggered.connect(add_project)
    menu.addAction(add_action)
    menu.addSeparator()
    exit_action = QAction("Exit tray")
    exit_action.triggered.connect(app.quit)
    menu.addAction(exit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: show_status() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    )
    timer = QTimer()
    timer.timeout.connect(poll_approval)
    timer.start(750)
    tray.show()
    if arguments.show_status:
        QTimer.singleShot(0, show_status)
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
