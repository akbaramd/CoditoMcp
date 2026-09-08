from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .config import AgentConfig
from .errors import AgentError
from .ipc import NamedPipeClient
from .updates import GitHubUpdateService, ReleaseUpdate

APP_STYLE = """
QWidget {
    color: #172033;
    font-family: "Segoe UI Variable", "Segoe UI";
    font-size: 13px;
}
QMainWindow, QWidget#AppRoot, QStackedWidget { background: #f4f7fb; }
QFrame#Sidebar {
    background: #101828;
    border: none;
    border-radius: 18px;
}
QLabel#BrandMark {
    background: #6ce9a6;
    color: #0b3b29;
    border-radius: 10px;
    font-size: 18px;
    font-weight: 800;
}
QLabel#BrandTitle { color: #ffffff; font-size: 18px; font-weight: 700; }
QLabel#BrandSubtitle, QLabel#SidebarFooter { color: #98a2b3; font-size: 11px; }
QPushButton[nav="true"] {
    background: transparent;
    color: #98a2b3;
    border: none;
    border-radius: 9px;
    padding: 11px 14px;
    text-align: left;
    font-weight: 600;
}
QPushButton[nav="true"]:hover { background: #1d2939; color: #ffffff; }
QPushButton[nav="true"]:checked { background: #344054; color: #ffffff; }
QLabel#Eyebrow { color: #667085; font-size: 11px; font-weight: 700; }
QLabel#PageTitle { color: #101828; font-size: 26px; font-weight: 750; }
QLabel#PageSubtitle { color: #667085; font-size: 13px; }
QLabel#SectionTitle { color: #101828; font-size: 16px; font-weight: 700; }
QLabel#Muted { color: #667085; }
QLabel#SmallMuted { color: #667085; font-size: 11px; }
QLabel#MetricValue { color: #101828; font-size: 21px; font-weight: 750; }
QLabel#MetricLabel { color: #667085; font-size: 11px; font-weight: 600; }
QLabel#HeroTitle { color: #ffffff; font-size: 25px; font-weight: 750; }
QLabel#HeroText { color: #d0d5dd; font-size: 13px; }
QLabel#HeroBadge {
    background: #1d2939;
    color: #6ce9a6;
    border: 1px solid #344054;
    border-radius: 12px;
    padding: 7px 11px;
    font-weight: 650;
}
QLabel#StatusOnline {
    background: #dcfae6;
    color: #067647;
    border: 1px solid #abefc6;
    border-radius: 11px;
    padding: 5px 10px;
    font-weight: 700;
}
QLabel#StatusOffline {
    background: #fee4e2;
    color: #b42318;
    border: 1px solid #fecdca;
    border-radius: 11px;
    padding: 5px 10px;
    font-weight: 700;
}
QFrame#Card, QFrame#MetricCard {
    background: #ffffff;
    border: 1px solid #e4e7ec;
    border-radius: 14px;
}
QFrame#Hero {
    background: #101828;
    border: none;
    border-radius: 16px;
}
QFrame#InfoBanner {
    background: #eff8ff;
    border: 1px solid #b2ddff;
    border-radius: 11px;
}
QPushButton {
    background: #ffffff;
    color: #344054;
    border: 1px solid #d0d5dd;
    border-radius: 8px;
    padding: 8px 13px;
    font-weight: 650;
}
QPushButton:hover { background: #f9fafb; border-color: #98a2b3; }
QPushButton:pressed { background: #f2f4f7; }
QPushButton[primary="true"] {
    background: #1570ef;
    color: #ffffff;
    border-color: #1570ef;
}
QPushButton[primary="true"]:hover { background: #175cd3; border-color: #175cd3; }
QLineEdit, QComboBox {
    background: #ffffff;
    border: 1px solid #d0d5dd;
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: #84adff;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #84adff; }
QTableWidget {
    background: #ffffff;
    alternate-background-color: #f9fafb;
    border: 1px solid #e4e7ec;
    border-radius: 11px;
    gridline-color: #eaecf0;
    selection-background-color: #eff8ff;
    selection-color: #101828;
}
QHeaderView::section {
    background: #f9fafb;
    color: #667085;
    border: none;
    border-bottom: 1px solid #eaecf0;
    padding: 9px;
    font-size: 11px;
    font-weight: 700;
}
QTableWidget::item { padding: 8px; border-bottom: 1px solid #f2f4f7; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #d0d5dd; border-radius: 4px; min-height: 28px; }
QStatusBar { background: #ffffff; color: #667085; border-top: 1px solid #eaecf0; }
"""

MODE_LABELS = {
    "isolated": "Isolated",
    "native_approval": "Native approval",
    "native_trusted": "Native trusted",
}
STATE_LABELS = {
    "received": "Received",
    "running": "Running",
    "waiting_approval": "Waiting approval",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "denied": "Denied",
    "cancelled": "Cancelled",
    "outcome_unknown": "Outcome unknown",
}
STATE_COLORS = {
    "succeeded": "#067647",
    "running": "#175cd3",
    "waiting_approval": "#b54708",
    "failed": "#b42318",
    "denied": "#b42318",
    "cancelled": "#667085",
    "outcome_unknown": "#b54708",
    "received": "#344054",
}


def _short_id(value: object) -> str:
    text = str(value or "")
    if len(text) <= 18:
        return text or "Not available"
    return f"{text[:10]}…{text[-6:]}"


def _relative_time(value: object) -> str:
    try:
        timestamp = datetime.fromisoformat(str(value)).astimezone(UTC)
    except (TypeError, ValueError):
        return "—"
    seconds = max(0, int((datetime.now(UTC) - timestamp).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86_400:
        return f"{seconds // 3600} hr ago"
    return f"{seconds // 86_400} days ago"


def _label(text: str, object_name: str | None = None) -> QLabel:
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    return label


def _card_layout(frame: QFrame, margins: int = 18) -> QVBoxLayout:
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(11)
    return layout


class MetricCard(QFrame):
    def __init__(self, label: str, value: str, detail: str) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        layout = _card_layout(self, 16)
        self.value = _label(value, "MetricValue")
        layout.addWidget(self.value)
        layout.addWidget(_label(label.upper(), "MetricLabel"))
        self.detail = _label(detail, "SmallMuted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)


class UpdateWorker(QThread):
    checked = Signal(object)
    install_started = Signal(str)
    failed = Signal(str)

    def __init__(self, update: ReleaseUpdate | None = None) -> None:
        super().__init__()
        self.update = update

    def run(self) -> None:
        try:
            service = GitHubUpdateService()
            if self.update is None:
                self.checked.emit(service.check())
            else:
                service.download_and_launch(self.update)
                self.install_started.emit(self.update.latest_version)
        except AgentError as exc:
            self.failed.emit(exc.message)
        except Exception:
            self.failed.emit("The update operation failed unexpectedly")


class CoditoMainWindow(QMainWindow):
    def __init__(
        self,
        config: AgentConfig,
        client: NamedPipeClient,
        tray: QSystemTrayIcon,
    ) -> None:
        super().__init__()
        self.config = config
        self.client = client
        self.tray = tray
        self._status: dict[str, Any] = {}
        self._projects: list[dict[str, Any]] = []
        self._rendered_project_signature: tuple[tuple[object, ...], ...] | None = None
        self._update_worker: UpdateWorker | None = None
        self._pending_update: ReleaseUpdate | None = None
        self._update_check_silent = False
        self._quitting = False
        self.setWindowTitle("Codito — Device MCP")
        self.setMinimumSize(920, 620)
        self.resize(1120, 740)
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self._build_ui()
        self._wire_tray()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(2500)
        self.approval_timer = QTimer(self)
        self.approval_timer.timeout.connect(self.poll_approval)
        self.approval_timer.start(750)
        self.project_request_timer = QTimer(self)
        self.project_request_timer.timeout.connect(self.poll_project_request)
        self.project_request_timer.start(1200)
        self.refresh()
        QTimer.singleShot(5000, self._check_automatic_update)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(20)
        root_layout.addWidget(self._build_sidebar())

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 7, 0, 0)
        content_layout.setSpacing(15)
        content_layout.addLayout(self._build_header())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_overview_page())
        self.pages.addWidget(self._build_projects_page())
        self.pages.addWidget(self._build_activity_page())
        self.pages.addWidget(self._build_settings_page())
        content_layout.addWidget(self.pages, 1)
        root_layout.addWidget(content, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Codito desktop is ready")

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(17, 20, 17, 18)
        layout.setSpacing(8)

        brand = QHBoxLayout()
        mark = _label("C", "BrandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(40, 40)
        brand.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(_label("Codito", "BrandTitle"))
        names.addWidget(_label("DEVICE BRIDGE", "BrandSubtitle"))
        brand.addLayout(names)
        brand.addStretch()
        layout.addLayout(brand)
        layout.addSpacing(22)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        navigation = [
            ("Overview", QStyle.StandardPixmap.SP_ComputerIcon),
            ("Projects", QStyle.StandardPixmap.SP_DirIcon),
            ("Activity", QStyle.StandardPixmap.SP_FileDialogDetailedView),
            ("Settings", QStyle.StandardPixmap.SP_FileDialogInfoView),
        ]
        for index, (title, icon_type) in enumerate(navigation):
            button = QPushButton(title)
            button.setProperty("nav", True)
            button.setCheckable(True)
            button.setIcon(self.style().standardIcon(icon_type))
            button.setIconSize(QSize(18, 18))
            button.clicked.connect(lambda checked=False, page=index: self._show_page(page))
            self.nav_group.addButton(button, index)
            layout.addWidget(button)
            if index == 0:
                button.setChecked(True)
        layout.addStretch()

        self.sidebar_status = _label("●  Checking connection", "SidebarFooter")
        layout.addWidget(self.sidebar_status)
        footer = _label("Secure local access\nCodito MVP 0.1.0", "SidebarFooter")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        return sidebar

    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.header_eyebrow = _label("WINDOWS DEVICE", "Eyebrow")
        self.header_title = _label("Overview", "PageTitle")
        titles.addWidget(self.header_eyebrow)
        titles.addWidget(self.header_title)
        layout.addLayout(titles)
        layout.addStretch()
        self.header_device = _label("Loading device…", "Muted")
        layout.addWidget(self.header_device)
        self.status_pill = _label("CHECKING", "StatusOffline")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_pill)
        return layout

    def _build_overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        hero = QFrame()
        hero.setObjectName("Hero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(24, 21, 24, 21)
        copy = QVBoxLayout()
        copy.setSpacing(6)
        copy.addWidget(_label("Your development workspace,\navailable to ChatGPT.", "HeroTitle"))
        hero_text = _label(
            "Codito keeps absolute paths on this device and routes only authorized MCP calls.",
            "HeroText",
        )
        hero_text.setWordWrap(True)
        copy.addWidget(hero_text)
        hero_layout.addLayout(copy, 1)
        tools = QVBoxLayout()
        for name in ("project_read", "project_apply_patch", "project_shell"):
            badge = _label(name, "HeroBadge")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tools.addWidget(badge)
        hero_layout.addLayout(tools)
        layout.addWidget(hero)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.connection_metric = MetricCard("Relay connection", "Checking", "Secure outbound WSS")
        self.projects_metric = MetricCard("Projects", "0", "Registered on this device")
        self.approvals_metric = MetricCard("Approvals", "0", "Waiting for your decision")
        metrics.addWidget(self.connection_metric)
        metrics.addWidget(self.projects_metric)
        metrics.addWidget(self.approvals_metric)
        layout.addLayout(metrics)

        connection = QFrame()
        connection.setObjectName("Card")
        connection_layout = _card_layout(connection)
        top = QHBoxLayout()
        top.addWidget(_label("Device MCP endpoint", "SectionTitle"))
        top.addStretch()
        self.copy_button = QPushButton("Copy link")
        self.copy_button.clicked.connect(self.copy_mcp_url)
        top.addWidget(self.copy_button)
        connection_layout.addLayout(top)
        connection_layout.addWidget(
            _label("Use this revocable URL when adding Codito in ChatGPT Developer Mode.", "Muted")
        )
        self.mcp_url = QLineEdit()
        self.mcp_url.setReadOnly(True)
        self.mcp_url.setPlaceholderText("Complete device enrollment to create an MCP link")
        connection_layout.addWidget(self.mcp_url)

        identity = QGridLayout()
        identity.setHorizontalSpacing(28)
        identity.setVerticalSpacing(6)
        identity.addWidget(_label("DEVICE", "MetricLabel"), 0, 0)
        identity.addWidget(_label("ACCOUNT", "MetricLabel"), 0, 1)
        identity.addWidget(_label("CONNECTION EPOCH", "MetricLabel"), 0, 2)
        self.device_value = _label("—")
        self.account_value = _label("—")
        self.epoch_value = _label("—")
        identity.addWidget(self.device_value, 1, 0)
        identity.addWidget(self.account_value, 1, 1)
        identity.addWidget(self.epoch_value, 1, 2)
        connection_layout.addLayout(identity)
        layout.addWidget(connection)

        banner = QFrame()
        banner.setObjectName("InfoBanner")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(15, 12, 15, 12)
        banner_layout.addWidget(_label("Security", "SectionTitle"))
        text = _label(
            "Isolated projects fail closed unless the Windows sandbox broker proves containment. "
            "Native commands always follow the selected local approval policy.",
            "Muted",
        )
        text.setWordWrap(True)
        banner_layout.addWidget(text, 1)
        layout.addWidget(banner)
        layout.addStretch()
        return page

    def _build_projects_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        toolbar = QHBoxLayout()
        description = _label(
            "Only fixed local folders are registered. Absolute paths never leave this device.",
            "Muted",
        )
        toolbar.addWidget(description)
        toolbar.addStretch()
        open_button = QPushButton("Open folder")
        open_button.clicked.connect(self.open_selected_project)
        toolbar.addWidget(open_button)
        rename_button = QPushButton("Rename")
        rename_button.clicked.connect(self.rename_selected_project)
        toolbar.addWidget(rename_button)
        self.availability_button = QPushButton("Remove from ChatGPT")
        self.availability_button.clicked.connect(self.toggle_selected_project)
        toolbar.addWidget(self.availability_button)
        add_button = QPushButton("Add project")
        add_button.setProperty("primary", True)
        add_button.clicked.connect(self.add_project)
        toolbar.addWidget(add_button)
        layout.addLayout(toolbar)

        self.projects_table = QTableWidget(0, 4)
        self.projects_table.setHorizontalHeaderLabels(
            ["PROJECT", "LOCAL FOLDER", "EXECUTION POLICY", "STATUS"]
        )
        self.projects_table.setAlternatingRowColors(True)
        self.projects_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.projects_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.projects_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.projects_table.verticalHeader().setVisible(False)
        self.projects_table.verticalHeader().setDefaultSectionSize(52)
        header = self.projects_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.projects_table.doubleClicked.connect(self.open_selected_project)
        self.projects_table.itemSelectionChanged.connect(self._project_selection_changed)
        layout.addWidget(self.projects_table, 1)
        return page

    def _build_activity_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        description = _label(
            "Recent operations are read from the durable local journal. "
            "File contents and tokens are not shown.",
            "Muted",
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.activity_table = QTableWidget(0, 5)
        self.activity_table.setHorizontalHeaderLabels(
            ["WHEN", "TOOL", "PROJECT", "STATE", "RELAY ACK"]
        )
        self.activity_table.setAlternatingRowColors(True)
        self.activity_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.activity_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.activity_table.verticalHeader().setVisible(False)
        self.activity_table.verticalHeader().setDefaultSectionSize(45)
        header = self.activity_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.activity_table, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(13)

        endpoint = QFrame()
        endpoint.setObjectName("Card")
        endpoint_layout = _card_layout(endpoint)
        endpoint_layout.addWidget(_label("Relay and local storage", "SectionTitle"))
        endpoint_layout.addWidget(_label("PUBLIC RELAY", "MetricLabel"))
        relay_value = QLineEdit(self.config.relay_http_url)
        relay_value.setReadOnly(True)
        endpoint_layout.addWidget(relay_value)
        endpoint_layout.addWidget(_label("LOCAL DATA DIRECTORY", "MetricLabel"))
        data_value = QLineEdit(str(self.config.data_directory))
        data_value.setReadOnly(True)
        endpoint_layout.addWidget(data_value)
        actions = QHBoxLayout()
        dashboard = QPushButton("Open web dashboard")
        dashboard.clicked.connect(self.open_dashboard)
        actions.addWidget(dashboard)
        copy_device = QPushButton("Copy device ID")
        copy_device.clicked.connect(self.copy_device_id)
        actions.addWidget(copy_device)
        reconnect = QPushButton("Reconnect agent")
        reconnect.clicked.connect(self.reconnect_agent)
        actions.addWidget(reconnect)
        stop = QPushButton("Stop agent")
        stop.clicked.connect(self.stop_agent)
        actions.addWidget(stop)
        actions.addStretch()
        endpoint_layout.addLayout(actions)
        layout.addWidget(endpoint)

        updates = QFrame()
        updates.setObjectName("Card")
        updates_layout = _card_layout(updates)
        update_header = QHBoxLayout()
        update_header.addWidget(_label("Updates", "SectionTitle"))
        update_header.addStretch()
        self.update_status = _label(f"Version {__version__}", "Muted")
        update_header.addWidget(self.update_status)
        updates_layout.addLayout(update_header)
        updates_layout.addWidget(
            _label(
                "Stable releases are retrieved from GitHub and verified with their published "
                "SHA-256 digest before installation.",
                "Muted",
            )
        )
        update_actions = QHBoxLayout()
        self.auto_update_checkbox = QCheckBox("Automatically check for stable updates")
        self.auto_update_checkbox.toggled.connect(self.set_auto_update)
        update_actions.addWidget(self.auto_update_checkbox)
        update_actions.addStretch()
        self.update_button = QPushButton("Check for updates")
        self.update_button.clicked.connect(lambda: self.check_for_updates(silent=False))
        update_actions.addWidget(self.update_button)
        updates_layout.addLayout(update_actions)
        layout.addWidget(updates)

        limits = QFrame()
        limits.setObjectName("Card")
        limits_layout = _card_layout(limits)
        limits_layout.addWidget(_label("MVP safety boundaries", "SectionTitle"))
        boundaries = [
            "No elevation, interactive terminal, PTY, remote GUI, or detached process.",
            "Native approval runs with your Windows account only after a one-shot local decision.",
            "Credentials are protected for the current Windows user; project roots remain local.",
            "A failed sandbox broker never falls back silently to native execution.",
        ]
        for value in boundaries:
            item = _label(f"✓  {value}", "Muted")
            item.setWordWrap(True)
            limits_layout.addWidget(item)
        layout.addWidget(limits)
        layout.addStretch()
        return page

    def _wire_tray(self) -> None:
        menu = QMenu()
        open_action = QAction("Open Codito", self)
        open_action.triggered.connect(self.show_window)
        menu.addAction(open_action)
        copy_action = QAction("Copy MCP link", self)
        copy_action.triggered.connect(self.copy_mcp_url)
        menu.addAction(copy_action)
        add_action = QAction("Add project…", self)
        add_action.triggered.connect(self.add_project)
        menu.addAction(add_action)
        menu.addSeparator()
        dashboard_action = QAction("Open web dashboard", self)
        dashboard_action.triggered.connect(self.open_dashboard)
        menu.addAction(dashboard_action)
        menu.addSeparator()
        quit_action = QAction("Exit Codito UI", self)
        quit_action.triggered.connect(self.quit_ui)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)

    def _show_page(self, index: int) -> None:
        names = ("Overview", "Projects", "Activity", "Settings")
        self.pages.setCurrentIndex(index)
        self.header_title.setText(names[index])
        button = self.nav_group.button(index)
        if button is not None:
            button.setChecked(True)
        if index == 2:
            self.refresh_activity()

    def _request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self.client.request(payload)
        except Exception:
            return None
        return response if isinstance(response, dict) else None

    def refresh(self) -> None:
        response = self._request({"action": "status"})
        if not response or not response.get("ok"):
            self._set_offline("Daemon unavailable")
            return
        self._status = response
        self._projects = [
            project for project in response.get("projects", []) if isinstance(project, dict)
        ]
        online = bool(response.get("online"))
        self._set_connection_state(online)
        self.header_device.setText(str(response.get("device_name") or "Windows device"))
        self.connection_metric.value.setText("Online" if online else "Offline")
        reason = response.get("last_disconnect_reason")
        self.connection_metric.detail.setText(
            "Secure outbound WSS is connected"
            if online
            else f"Reconnect is automatic{f' · {reason}' if reason else ''}"
        )
        self.projects_metric.value.setText(str(len(self._projects)))
        isolated = sum(1 for project in self._projects if project.get("mode") == "isolated")
        self.projects_metric.detail.setText(f"{isolated} isolated · paths remain local")
        pending = int(response.get("pending_approvals") or 0)
        self.approvals_metric.value.setText(str(pending))
        self.approvals_metric.detail.setText(
            "Action required" if pending else "No decisions waiting"
        )
        self.mcp_url.setText(str(response.get("mcp_url") or ""))
        self.copy_button.setEnabled(bool(response.get("mcp_url")))
        self.device_value.setText(_short_id(response.get("device_id")))
        self.account_value.setText(_short_id(response.get("account_id")))
        self.epoch_value.setText(str(response.get("connection_epoch") or "—"))
        self.auto_update_checkbox.blockSignals(True)
        self.auto_update_checkbox.setChecked(bool(response.get("auto_update")))
        self.auto_update_checkbox.blockSignals(False)
        self._populate_projects()
        if self.pages.currentIndex() == 2:
            self.refresh_activity()
        self.statusBar().showMessage(f"Updated {datetime.now().strftime('%H:%M:%S')}", 1500)

    def _set_connection_state(self, online: bool) -> None:
        self.status_pill.setText("●  ONLINE" if online else "●  OFFLINE")
        self.status_pill.setObjectName("StatusOnline" if online else "StatusOffline")
        self.status_pill.style().unpolish(self.status_pill)
        self.status_pill.style().polish(self.status_pill)
        self.sidebar_status.setText("●  Relay connected" if online else "●  Relay offline")
        self.sidebar_status.setStyleSheet(f"color: {'#6ce9a6' if online else '#fda29b'};")
        self.tray.setToolTip("Codito — Online" if online else "Codito — Offline")

    def _set_offline(self, detail: str) -> None:
        self._status = {}
        self._projects = []
        self._set_connection_state(False)
        self.header_device.setText("Daemon unavailable")
        self.connection_metric.value.setText("Offline")
        self.connection_metric.detail.setText(detail)
        self.projects_metric.value.setText("—")
        self.approvals_metric.value.setText("—")
        self.mcp_url.clear()
        self.copy_button.setEnabled(False)
        self._populate_projects()

    def _populate_projects(self) -> None:
        signature = tuple(
            (
                project.get("project_id"),
                project.get("title"),
                project.get("root"),
                project.get("mode"),
                project.get("enabled", True),
            )
            for project in self._projects
        )
        if signature == self._rendered_project_signature:
            return
        self._rendered_project_signature = signature
        selected_id = None
        selected = self.projects_table.selectedItems()
        if selected:
            selected_id = selected[0].data(Qt.ItemDataRole.UserRole)
        self.projects_table.setRowCount(len(self._projects))
        for row, project in enumerate(self._projects):
            project_id = str(project.get("project_id") or "")
            title = QTableWidgetItem(str(project.get("title") or "Untitled"))
            title.setData(Qt.ItemDataRole.UserRole, project_id)
            title.setData(Qt.ItemDataRole.UserRole + 1, str(project.get("root") or ""))
            self.projects_table.setItem(row, 0, title)
            path = QTableWidgetItem(str(project.get("root") or ""))
            path.setToolTip(str(project.get("root") or ""))
            self.projects_table.setItem(row, 1, path)

            mode = QComboBox()
            for value, label in MODE_LABELS.items():
                mode.addItem(label, value)
            current = mode.findData(str(project.get("mode") or "isolated"))
            mode.setCurrentIndex(max(0, current))
            mode.currentIndexChanged.connect(
                lambda index, item=mode, pid=project_id: self.change_project_mode(
                    pid, str(item.itemData(index)), item
                )
            )
            self.projects_table.setCellWidget(row, 2, mode)
            status = QTableWidgetItem("Available" if project.get("enabled", True) else "Paused")
            status.setForeground(QColor("#067647" if project.get("enabled", True) else "#667085"))
            self.projects_table.setItem(row, 3, status)
            if project_id == selected_id:
                self.projects_table.selectRow(row)

    def refresh_activity(self) -> None:
        response = self._request({"action": "activity.list", "limit": 75})
        activity = response.get("activity", []) if response and response.get("ok") else []
        values = [item for item in activity if isinstance(item, dict)]
        self.activity_table.setRowCount(len(values))
        for row, item in enumerate(values):
            state = str(item.get("state") or "")
            error = str(item.get("error_code") or "")
            cells = [
                _relative_time(item.get("updated_at")),
                str(item.get("capability") or "—"),
                str(item.get("project_title") or "Device"),
                f"{STATE_LABELS.get(state, state)}{f' · {error}' if error else ''}",
                "Acknowledged" if item.get("terminal_acked") else "Pending",
            ]
            for column, value in enumerate(cells):
                cell = QTableWidgetItem(value)
                if column == 3:
                    cell.setForeground(QColor(STATE_COLORS.get(state, "#344054")))
                self.activity_table.setItem(row, column, cell)

    def add_project(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select a fixed local project directory", str(Path.home())
        )
        if not directory:
            return
        suggested = Path(directory).name or "Project"
        title, accepted = QInputDialog.getText(
            self, "Project title", "Title shown to ChatGPT:", text=suggested
        )
        if not accepted:
            return
        response = self._request(
            {"action": "project.register", "title": title.strip(), "path": directory}
        )
        if not response or not response.get("ok"):
            self._show_error(response, "The project could not be registered.")
            return
        self.refresh()
        self.statusBar().showMessage(f"Added {title.strip()}", 3000)

    def change_project_mode(self, project_id: str, mode: str, combo: QComboBox) -> None:
        project = next(
            (value for value in self._projects if value.get("project_id") == project_id), None
        )
        previous = str(project.get("mode") or "isolated") if project else "isolated"
        if mode == previous:
            return
        acknowledged = False
        if mode == "native_trusted":
            answer = QMessageBox.warning(
                self,
                "Enable Native trusted?",
                "Native trusted commands run with your full Windows user filesystem and network "
                "authority without a local prompt. A project working directory is not a security "
                "boundary.\n\nEnable this mode only for a project and account you fully trust.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(previous))
                combo.blockSignals(False)
                return
            acknowledged = True
        response = self._request(
            {
                "action": "project.mode",
                "project_id": project_id,
                "mode": mode,
                "acknowledge_full_user_authority": acknowledged,
            }
        )
        if not response or not response.get("ok"):
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(previous))
            combo.blockSignals(False)
            self._show_error(response, "The execution policy could not be changed.")
            return
        if project is not None:
            project["mode"] = mode
        self._rendered_project_signature = None
        self.statusBar().showMessage(f"Execution policy changed to {MODE_LABELS[mode]}", 3000)

    def _selected_project(self) -> dict[str, Any] | None:
        selected = self.projects_table.selectedItems()
        if not selected:
            return None
        project_id = str(selected[0].data(Qt.ItemDataRole.UserRole) or "")
        return next(
            (item for item in self._projects if str(item.get("project_id")) == project_id), None
        )

    def _project_selection_changed(self) -> None:
        project = self._selected_project()
        enabled = bool(project.get("enabled", True)) if project else True
        self.availability_button.setText("Remove from ChatGPT" if enabled else "Restore to ChatGPT")

    def rename_selected_project(self) -> None:
        project = self._selected_project()
        if project is None:
            self.statusBar().showMessage("Select a project first", 2500)
            return
        title, accepted = QInputDialog.getText(
            self,
            "Rename project",
            "Name shown to ChatGPT:",
            text=str(project.get("title") or ""),
        )
        if not accepted:
            return
        response = self._request(
            {
                "action": "project.rename",
                "project_id": project.get("project_id"),
                "title": title.strip(),
            }
        )
        if not response or not response.get("ok"):
            self._show_error(response, "The project could not be renamed.")
            return
        self._rendered_project_signature = None
        self.refresh()
        self.statusBar().showMessage("Project renamed and synchronized", 3000)

    def toggle_selected_project(self) -> None:
        project = self._selected_project()
        if project is None:
            self.statusBar().showMessage("Select a project first", 2500)
            return
        currently_enabled = bool(project.get("enabled", True))
        if currently_enabled:
            answer = QMessageBox.question(
                self,
                "Remove project from ChatGPT?",
                "This unregisters the project from Codito and cancels its local approvals. "
                "No source files or folders will be deleted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        response = self._request(
            {
                "action": "project.enabled",
                "project_id": project.get("project_id"),
                "enabled": not currently_enabled,
            }
        )
        if not response or not response.get("ok"):
            self._show_error(response, "The project availability could not be changed.")
            return
        self._rendered_project_signature = None
        self.refresh()
        state = "restored" if currently_enabled is False else "removed"
        self.statusBar().showMessage(f"Project {state}; local files were not changed", 3500)

    def reconnect_agent(self) -> None:
        response = self._request({"action": "connection.reconnect"})
        if not response or not response.get("ok"):
            self._show_error(response, "The agent connection could not be restarted.")
            return
        self.statusBar().showMessage("A fresh secure connection was requested", 3500)

    def stop_agent(self) -> None:
        answer = QMessageBox.question(
            self,
            "Stop Codito agent?",
            "ChatGPT will lose access until the daemon starts again. Running shell jobs and "
            "pending approvals will be cancelled.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        response = self._request({"action": "shutdown"})
        if not response or not response.get("ok"):
            self._show_error(response, "The agent could not be stopped.")
            return
        self.statusBar().showMessage("Codito agent is stopping", 3500)

    def set_auto_update(self, enabled: bool) -> None:
        response = self._request({"action": "setting.auto_update", "enabled": enabled})
        if not response or not response.get("ok"):
            self.auto_update_checkbox.blockSignals(True)
            self.auto_update_checkbox.setChecked(not enabled)
            self.auto_update_checkbox.blockSignals(False)
            self._show_error(response, "The update preference could not be saved.")
            return
        self.statusBar().showMessage(
            "Automatic update checks enabled" if enabled else "Automatic update checks disabled",
            3000,
        )

    def _check_automatic_update(self) -> None:
        if bool(self._status.get("auto_update")):
            self.check_for_updates(silent=True)

    def check_for_updates(self, *, silent: bool) -> None:
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        self._update_check_silent = silent
        self.update_button.setEnabled(False)
        self.update_status.setText("Checking GitHub…")
        worker = UpdateWorker()
        worker.checked.connect(self._update_checked)
        worker.failed.connect(self._update_failed)
        worker.finished.connect(self._update_finished)
        self._update_worker = worker
        worker.start()

    def _update_checked(self, value: object) -> None:
        if not isinstance(value, ReleaseUpdate):
            self._update_failed("GitHub returned an invalid update response")
            return
        if not value.available:
            self.update_status.setText(f"Version {__version__} · Up to date")
            if not self._update_check_silent:
                QMessageBox.information(self, "Codito updates", "Codito is up to date.")
            return
        self.update_status.setText(f"Version {value.latest_version} available")
        answer = QMessageBox.question(
            self,
            "Install Codito update?",
            f"Codito {value.latest_version} is available. The {value.asset_size / 1_048_576:.1f} "
            "MiB package will be verified, installed for this Windows user, and the agent will "
            "restart.\n\nDownload and install now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._pending_update = value

    def _start_update_install(self, update: ReleaseUpdate) -> None:
        self._update_check_silent = False
        self.update_button.setEnabled(False)
        self.update_status.setText(f"Downloading {update.latest_version}…")
        worker = UpdateWorker(update)
        worker.install_started.connect(self._update_install_started)
        worker.failed.connect(self._update_failed)
        worker.finished.connect(self._update_finished)
        self._update_worker = worker
        worker.start()

    def _update_install_started(self, version: str) -> None:
        self.update_status.setText(f"Installing {version}…")
        self.statusBar().showMessage("Verified update launched; Codito will restart", 5000)

    def _update_failed(self, message: str) -> None:
        self.update_status.setText(f"Version {__version__} · Check failed")
        if not self._update_check_silent:
            QMessageBox.warning(self, "Codito updates", message)

    def _update_finished(self) -> None:
        worker = self._update_worker
        pending = self._pending_update if worker is not None and worker.update is None else None
        self._pending_update = None
        self._update_worker = None
        self.update_button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()
        if pending is not None:
            QTimer.singleShot(0, lambda: self._start_update_install(pending))

    def open_selected_project(self, *_: object) -> None:
        selected = self.projects_table.selectedItems()
        if not selected:
            self.statusBar().showMessage("Select a project first", 2500)
            return
        path = str(selected[0].data(Qt.ItemDataRole.UserRole + 1) or "")
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def copy_mcp_url(self) -> None:
        value = str(self._status.get("mcp_url") or self.mcp_url.text())
        if not value:
            self.statusBar().showMessage("No MCP link is available yet", 2500)
            return
        QApplication.clipboard().setText(value)
        self.statusBar().showMessage("MCP link copied", 2500)

    def copy_device_id(self) -> None:
        value = str(self._status.get("device_id") or "")
        if not value:
            self.statusBar().showMessage("No device ID is available yet", 2500)
            return
        QApplication.clipboard().setText(value)
        self.statusBar().showMessage("Device ID copied", 2500)

    def open_dashboard(self) -> None:
        QDesktopServices.openUrl(QUrl(self.config.relay_http_url))

    def poll_approval(self) -> None:
        response = self._request({"action": "approval.next"})
        approval = response.get("approval") if response else None
        if not isinstance(approval, dict):
            return
        self.tray.showMessage(
            "Codito approval required",
            f"{approval.get('project_title')}: {approval.get('summary')}",
            QSystemTrayIcon.MessageIcon.Warning,
            10_000,
        )
        self.show_window()
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Codito local approval")
        dialog.setText(str(approval.get("summary", "Remote operation")))
        dialog.setInformativeText(
            f"Project: {approval.get('project_title')}\n"
            f"Capability: {approval.get('capability')}\n"
            f"Risk: {approval.get('risk')}\n\n"
            "Review the exact operation details before allowing it."
        )
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
        self._request(
            {
                "action": "approval.respond",
                "request_id": approval["request_id"],
                "decision": decision,
            }
        )
        self.refresh()

    def poll_project_request(self) -> None:
        response = self._request({"action": "project.request.next"})
        request = response.get("request") if response else None
        if not isinstance(request, dict):
            return
        self.tray.showMessage(
            "ChatGPT requested a project",
            f"Select the local folder for {request.get('title', 'New project')}",
            QSystemTrayIcon.MessageIcon.Information,
            10_000,
        )
        self.show_window()
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setWindowTitle("Add a project requested by ChatGPT")
        dialog.setText(str(request.get("title") or "New project"))
        dialog.setInformativeText(
            "ChatGPT can suggest a display name, but only you can select a local folder. "
            "The absolute path will stay on this device."
        )
        dismiss = dialog.addButton("Dismiss", QMessageBox.ButtonRole.RejectRole)
        select = dialog.addButton("Select local folder…", QMessageBox.ButtonRole.AcceptRole)
        dialog.exec()
        request_id = str(request.get("request_id") or "")
        if dialog.clickedButton() is dismiss:
            self._request({"action": "project.request.dismiss", "request_id": request_id})
            return
        if dialog.clickedButton() is not select:
            return
        directory = QFileDialog.getExistingDirectory(
            self, "Select a fixed local project directory", str(Path.home())
        )
        if not directory:
            self._request({"action": "project.request.dismiss", "request_id": request_id})
            return
        completed = self._request(
            {
                "action": "project.request.complete",
                "request_id": request_id,
                "path": directory,
            }
        )
        if not completed or not completed.get("ok"):
            self._show_error(completed, "The requested project could not be registered.")
            return
        self._rendered_project_signature = None
        self._show_page(1)
        self.refresh()
        self.statusBar().showMessage("Project registered and synchronized with ChatGPT", 4000)

    def _show_error(self, response: dict[str, Any] | None, fallback: str) -> None:
        message = str(response.get("message") or fallback) if response else fallback
        code = str(response.get("error") or "ipc_unavailable") if response else "ipc_unavailable"
        QMessageBox.warning(self, "Codito", f"{message}\n\nError: {code}")

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.refresh()

    def quit_ui(self) -> None:
        self._quitting = True
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "Codito is still running",
            "Open Codito from the tray to review status and approvals.",
            QSystemTrayIcon.MessageIcon.Information,
            3500,
        )


def run_desktop(config: AgentConfig, client: NamedPipeClient, *, minimized: bool) -> int:
    app = QApplication.instance()
    if app is None:
        app = QApplication(["Codito"])
    elif not isinstance(app, QApplication):
        raise RuntimeError("Codito requires a QApplication instance")
    app.setApplicationName("Codito")
    app.setApplicationDisplayName("Codito Device MCP")
    app.setOrganizationName("Codito")
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(APP_STYLE)
    icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    app.setWindowIcon(icon)
    tray = QSystemTrayIcon(QIcon(icon), app)
    tray.setToolTip("Codito — Starting")
    window = CoditoMainWindow(config, client, tray)
    tray.show()
    if not minimized:
        QTimer.singleShot(0, window.show_window)
    return app.exec()
