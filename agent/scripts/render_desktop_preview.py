from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from codito_agent.config import AgentConfig
from codito_agent.desktop_ui import APP_STYLE, CoditoMainWindow
from codito_agent.ipc import IpcSecretStore, NamedPipeClient, default_pipe_name


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the live Codito desktop for visual QA")
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--page",
        choices=("overview", "projects", "activity", "settings"),
        default="overview",
    )
    arguments = parser.parse_args()

    config = AgentConfig.load()
    key = IpcSecretStore(config.data_directory / "ipc-key.dpapi").load_or_create()
    client = NamedPipeClient(default_pipe_name(), key)
    app = QApplication(["Codito visual QA"])
    app.setStyleSheet(APP_STYLE)
    tray = QSystemTrayIcon(QIcon(), app)
    window = CoditoMainWindow(config, client, tray)
    window.refresh_timer.stop()
    window.approval_timer.stop()
    window.project_request_timer.stop()
    window._show_page(("overview", "projects", "activity", "settings").index(arguments.page))
    window.show()
    app.processEvents()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    if not window.grab().save(str(arguments.output), "PNG"):
        raise SystemExit("Could not render the Codito UI preview")
    window._quitting = True
    window.close()


if __name__ == "__main__":
    main()
