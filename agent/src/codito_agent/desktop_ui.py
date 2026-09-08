from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QFont, QIcon, QScreen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
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
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .account import sign_in, sign_out
from .config import AgentConfig
from .diagnostics import event
from .errors import AgentError
from .ipc import NamedPipeClient
from .notifications import (
    ToastDelivery,
    dismiss_native_toast,
    register_activation,
    show_native_toast,
)
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
    "native_approval": "Ask for every operation",
    "native_project": "Project access",
    "full_access": "Full device access",
}
PROJECT_ACCESS_HELP = (
    "Ask for every operation: every operation requests local approval.\n"
    "Project access: files inside the project are free; outside access requires approval. "
    "Native shell uses conservative path detection, not OS confinement: a working directory "
    "does not prevent scripts or child processes accessing other paths.\n"
    "Full device access: no local prompts, including screenshots; full Windows user authority. "
    "OAuth scopes and locked-desktop protections still apply."
)
PermissionCategory = Literal["read", "shell", "screen"]
PERMISSION_LABELS = {
    "read": ("File reading", "files:read", "Read folder and descendants"),
    "shell": (
        "Native shell",
        "shell:execute",
        "Run native commands with full Windows user authority",
    ),
    "screen": ("Screenshots", "screen:read", "Capture selected monitor, including private windows"),
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


class MetricCard(QFrame):  # type: ignore[misc, unused-ignore]  # PySide wheel varies by runner.
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


class PermissionReviewDialog(QDialog):  # type: ignore[misc, unused-ignore]
    """Local-only grant inventory; revoke an exact row or one capability group."""

    def __init__(
        self,
        category: PermissionCategory,
        request: Callable[[dict[str, Any]], dict[str, Any] | None],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.category = category
        self._request = request
        self._permissions_current = False
        title, self.capability, self.action_label = PERMISSION_LABELS[category]
        self.setWindowTitle(f"Codito saved permissions — {title}")
        self.resize(980, 500)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Saved Always allow permissions are scoped to the shown account/connection. "
            "Revoking cancels pending approvals; it does not change a project's access mode. "
            "Full device access can still authorize operations without these saved permissions."
        )
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(explanation)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["SCOPE", "CAPABILITY", "ACTION", "ACCOUNT / LINK", "CREATED"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_permissions)
        buttons.addWidget(refresh)
        self.revoke_selected = QPushButton("Revoke selected")
        self.revoke_selected.clicked.connect(lambda: self.revoke(all_in_category=False))
        buttons.addWidget(self.revoke_selected)
        self.revoke_all = QPushButton(f"Revoke all {title.lower()} permissions")
        self.revoke_all.clicked.connect(lambda: self.revoke(all_in_category=True))
        buttons.addWidget(self.revoke_all)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh_permissions()

    def _selection_changed(self) -> None:
        selected = self.table.selectedItems()
        self.revoke_selected.setEnabled(
            self._permissions_current
            and bool(selected)
            and isinstance(selected[0].data(Qt.ItemDataRole.UserRole), str)
        )

    def refresh_permissions(self) -> None:
        response = self._request({"action": f"{self.category}_permissions.list"})
        values = response.get("permissions") if response and response.get("ok") else None
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            self._permissions_current = False
            self.status.setText(
                "Could not refresh permissions. Displayed rows may be stale; retry."
            )
            self.revoke_selected.setEnabled(False)
            self.revoke_all.setEnabled(False)
            return
        self._permissions_current = True
        self.table.setRowCount(0)
        self.table.setRowCount(len(values))
        for row, value in enumerate(values):
            scope = (
                f"{value.get('display_label', '')} · {value.get('display_id', '')}"
                if self.category == "screen"
                else str(value.get("scope_path") or "Unavailable")
            )
            columns = (
                scope,
                self.capability,
                self.action_label,
                f"{value.get('account_id', '')}\n{value.get('link_id', '')}",
                str(value.get("created_at") or ""),
            )
            for column, text in enumerate(columns):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, value.get("permission_id"))
                self.table.setItem(row, column, item)
        self.revoke_all.setEnabled(bool(values))
        self.revoke_selected.setEnabled(False)
        self.status.setText(
            f"{len(values)} saved permission(s). Select a row to revoke only that grant."
        )

    def revoke(self, *, all_in_category: bool) -> None:
        if not self._permissions_current:
            return
        payload: dict[str, Any] = {"action": f"{self.category}_permissions.revoke_all"}
        if not all_in_category:
            row = self.table.currentRow()
            item = self.table.item(row, 0) if row >= 0 else None
            permission_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if not isinstance(permission_id, str):
                self.status.setText(
                    "Select a current permission; refresh if its identifier is missing."
                )
                return
            payload = {
                "action": "permissions.revoke",
                "category": self.category,
                "permission_id": permission_id,
            }
        selection = (
            "all permissions in this category" if all_in_category else "the selected permission"
        )
        if (
            QMessageBox.question(
                self,
                "Revoke saved access?",
                f"Revoke {selection}? Pending local approvals will be cancelled. "
                "Project access modes are unchanged.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        response = self._request(payload)
        if not response or not response.get("ok"):
            self.status.setText(
                "Revocation failed. Access was not confirmed removed; refresh and retry."
            )
            return
        self.refresh_permissions()


class UpdateWorker(QThread):  # type: ignore[misc, unused-ignore]  # PySide wheel varies by runner.
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


class ApprovalCornerPanel(QFrame):  # type: ignore[misc, unused-ignore]
    """Clearly Codito-owned, nonmodal fallback; never activates the main window."""

    decision_chosen = Signal(str)
    review_requested = Signal()

    def __init__(self, approval: dict[str, Any]) -> None:
        super().__init__(None)
        self.setWindowTitle("Codito — Local approval")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setObjectName("CoditoApprovalPanel")
        self.setStyleSheet(
            "QFrame#CoditoApprovalPanel {background:#f6f7f9;border:1px solid #64748b;}"
            "QLabel {color:#172033;background:transparent;font-family:'Segoe UI';}"
            "QPushButton {padding:7px 9px;background:#ffffff;color:#172033;"
            "border:1px solid #94a3b8;border-radius:3px;}"
            "QPushButton:hover {background:#e2e8f0;}"
        )
        self.setFixedWidth(430)
        self._chosen = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        heading = QLabel("Codito · Local approval")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setStyleSheet("font-size:15px;font-weight:600;")
        layout.addWidget(heading)
        summary = QLabel(str(approval.get("summary", "Remote operation"))[:200])
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        detail.setFixedHeight(88)
        detail.setPlainText(
            json.dumps(
                {
                    key: approval.get(key)
                    for key in (
                        "account_id",
                        "grant_id",
                        "link_id",
                        "device_id",
                        "project_title",
                        "capability",
                        "risk",
                        "deadline_at",
                        "action_digest",
                        "working_directory",
                        "requested_external_paths",
                        "command",
                        "patch",
                        "requested_network",
                        "environment_differences",
                    )
                    if approval.get(key) is not None
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        layout.addWidget(detail)
        actions = [("Deny", "deny"), ("Allow", "allow_once")]
        warning = "Allow applies only to this request. Details opens the complete approval."
        if approval.get("persistent_screen_eligible"):
            actions.append(("Always allow", "allow_always_screen"))
            warning = (
                "Always allow: this monitor/account only, including private visible windows. "
                "Revoke in Settings."
            )
        elif approval.get("persistent_read_eligible"):
            actions.append(("Always allow", "allow_always_read"))
            warning = (
                "Always allow: read this folder and descendants, including private files. "
                "No edits or shell access."
            )
        elif approval.get("persistent_shell_eligible"):
            actions.append(("Always allow", "allow_always_shell"))
            warning = (
                "Always allow: native shell for this scope/account, with full Windows user "
                "authority. Revoke in Settings."
            )
        scope = QLabel(warning)
        scope.setTextFormat(Qt.TextFormat.PlainText)
        scope.setWordWrap(True)
        layout.addWidget(scope)
        buttons = QHBoxLayout()
        tokens = approval.get("toast_tokens", {})
        for label, decision in actions:
            button = QPushButton(label)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAutoDefault(False)
            button.setEnabled(isinstance(tokens.get(decision), str))
            button.clicked.connect(lambda _=False, chosen=decision: self._choose(chosen))
            buttons.addWidget(button)
        details = QPushButton("Details")
        details.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        details.setAutoDefault(False)
        details.clicked.connect(self.review_requested.emit)
        buttons.addWidget(details)
        layout.addLayout(buttons)

    def _choose(self, decision: str) -> None:
        if self._chosen:
            return
        self._chosen = True
        self.hide()
        self.decision_chosen.emit(decision)


class ApprovalToastWorker(QThread):  # type: ignore[misc, unused-ignore]
    failed = Signal()
    delivered = Signal(object)

    def __init__(self, broker: Path | None, approval: dict[str, Any]) -> None:
        super().__init__()
        self.broker = broker
        self.approval = approval

    def run(self) -> None:
        try:
            delivery = show_native_toast(self.broker, self.approval)
            event("toast_submitted", str(self.approval.get("request_id", "")))
            self.delivered.emit(delivery)
        except Exception as exc:
            event("toast_failed", str(self.approval.get("request_id", "")), type(exc).__name__)
            self.failed.emit()


class ApprovalDismissWorker(QThread):  # type: ignore[misc, unused-ignore]
    """Withdraw the parallel native notification without blocking the Qt loop."""

    def __init__(self, broker: Path | None, request_id: str) -> None:
        super().__init__()
        self.broker = broker
        self.request_id = request_id

    def run(self) -> None:
        dismiss_native_toast(self.broker, self.request_id)


class AccountWorker(QThread):  # type: ignore[misc, unused-ignore]
    completed = Signal(str, object)
    failed = Signal(str)

    def __init__(self, config: AgentConfig, action: str) -> None:
        super().__init__()
        self.config = config
        self.action = action

    def run(self) -> None:
        try:
            if self.action == "logout":
                asyncio.run(sign_out(self.config))
                result: object = None
            else:
                result = asyncio.run(sign_in(self.config, reenroll=self.action == "reenroll"))
            self.completed.emit(self.action, result)
        except AgentError as exc:
            self.failed.emit(exc.message)
        except Exception:
            self.failed.emit("The account operation failed unexpectedly")


class CoditoMainWindow(QMainWindow):  # type: ignore[misc, unused-ignore]  # PySide wheel varies.
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
        self._account_worker: AccountWorker | None = None
        self._pending_update: ReleaseUpdate | None = None
        self._update_check_silent = False
        self._quitting = False
        self._quit_requested_to_app = False
        self._approval_dialog_active = False
        self._toast_workers: list[ApprovalToastWorker | ApprovalDismissWorker] = []
        self._notified_approvals: set[str] = set()
        self._corner_pending: dict[str, tuple[dict[str, Any], float]] = {}
        self._corner_panels: dict[str, ApprovalCornerPanel] = {}
        self._corner_suspended_until = 0.0
        self._corner_deciding: set[str] = set()
        self._capture_in_progress = False
        try:
            self._actionable_toasts = register_activation()
        except Exception as exc:
            event("toast_registration_failed", detail=type(exc).__name__)
            self._actionable_toasts = False
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
        self.capture_timer = QTimer(self)
        self.capture_timer.timeout.connect(self.poll_capture)
        self.capture_timer.timeout.connect(self.poll_desktop_action)
        self.capture_timer.start(750)
        self.project_request_timer = QTimer(self)
        self.project_request_timer.timeout.connect(self.poll_project_request)
        self.project_request_timer.start(1200)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._check_automatic_update)
        self.update_timer.start(6 * 60 * 60 * 1000)
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
        footer = _label(f"Secure local access\nCodito MVP {__version__}", "SidebarFooter")
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
        for name in (
            "project_read",
            "project_apply_patch",
            "project_shell",
            "project_manage",
            "device_read",
            "device_screenshot",
        ):
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
        self.review_approvals_button = QPushButton("Review pending approvals")
        self.review_approvals_button.clicked.connect(self.review_pending_approvals)
        layout.addWidget(self.review_approvals_button)
        test_notification = QPushButton("Test Windows approval notification (no command)")
        test_notification.clicked.connect(lambda: self._request({"action": "approval.test"}))
        layout.addWidget(test_notification)

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
            "Choose each project's local access level. Native shell has your Windows account's "
            "authority; the working directory is not a sandbox. Legacy isolated projects stay "
            "blocked until you explicitly choose a supported access level.",
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
        access_help = _label(PROJECT_ACCESS_HELP, "Muted")
        access_help.setWordWrap(True)
        layout.addWidget(access_help)

        self.projects_table = QTableWidget(0, 4)
        self.projects_table.setHorizontalHeaderLabels(
            ["PROJECT", "LOCAL FOLDER", "ACCESS LEVEL", "STATUS"]
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
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(13)

        account = QFrame()
        account.setObjectName("Card")
        account_layout = _card_layout(account)
        account_header = QHBoxLayout()
        account_header.addWidget(_label("Account and device enrollment", "SectionTitle"))
        account_header.addStretch()
        self.account_state = _label("Checking local agent…", "Muted")
        account_header.addWidget(self.account_state)
        account_layout.addLayout(account_header)
        account_help = _label(
            "Sign-in uses your system browser and OAuth PKCE. Re-enrollment rotates a revoked "
            "device key, MCP link, and device-scoped project identifiers without deleting files.",
            "Muted",
        )
        account_help.setWordWrap(True)
        account_layout.addWidget(account_help)
        account_actions = QHBoxLayout()
        self.login_button = QPushButton("Sign in / refresh login")
        self.login_button.setProperty("primary", True)
        self.login_button.clicked.connect(lambda: self.begin_account_action("login"))
        account_actions.addWidget(self.login_button)
        self.reenroll_button = QPushButton("Re-enroll revoked device")
        self.reenroll_button.clicked.connect(lambda: self.begin_account_action("reenroll"))
        account_actions.addWidget(self.reenroll_button)
        self.logout_button = QPushButton("Sign out and revoke")
        self.logout_button.clicked.connect(lambda: self.begin_account_action("logout"))
        account_actions.addWidget(self.logout_button)
        account_actions.addStretch()
        account_layout.addLayout(account_actions)
        layout.addWidget(account)

        permissions = QFrame()
        permissions.setObjectName("Card")
        permissions_layout = _card_layout(permissions)
        permissions_layout.addWidget(_label("Saved Always allow permissions", "SectionTitle"))
        permissions_help = _label(
            "Always allow permits reading the selected folder and descendants for the approved "
            "account/link only. Reading, shell and screenshots are separate capabilities. "
            "Ask for every operation ignores these grants; "
            "Full device access does not require them.",
            "Muted",
        )
        permissions_help.setWordWrap(True)
        permissions_layout.addWidget(permissions_help)
        permissions_button = QPushButton("Review / revoke saved read permissions")
        permissions_button.clicked.connect(self.review_read_permissions)
        permissions_layout.addWidget(permissions_button)
        shell_permissions_button = QPushButton("Review / revoke saved shell permissions")
        shell_permissions_button.clicked.connect(self.review_shell_permissions)
        permissions_layout.addWidget(shell_permissions_button)
        screen_permissions_button = QPushButton("Review / revoke saved screenshot permissions")
        screen_permissions_button.clicked.connect(self.review_screen_permissions)
        permissions_layout.addWidget(screen_permissions_button)
        screen_help = _label(
            "Screenshot Always allow is separate: only the approved monitor and connection. "
            "It may reveal private windows. Changing monitors/layout requires new approval.",
            "Muted",
        )
        screen_help.setWordWrap(True)
        permissions_layout.addWidget(screen_help)
        layout.addWidget(permissions)

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
        self.auto_update_checkbox = QCheckBox("Automatically install stable updates")
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
            "Ask for every operation requests a new local decision; saved Always allow is ignored.",
            "Full device access removes local prompts, not OAuth scopes or locked-desktop checks.",
            "Credentials are protected for the current Windows user; project roots remain local.",
            "A failed sandbox broker never falls back silently to native execution.",
        ]
        for value in boundaries:
            item = _label(f"✓  {value}", "Muted")
            item.setWordWrap(True)
            limits_layout.addWidget(item)
        layout.addWidget(limits)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)
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
        self.tray.messageClicked.connect(self.review_pending_approvals)
        approvals_action = QAction("Review pending approvals", self)
        approvals_action.triggered.connect(self.review_pending_approvals)
        menu.addAction(approvals_action)

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
        except Exception as exc:
            event("ipc_error", detail=type(exc).__name__)
            return None
        return response if isinstance(response, dict) else None

    def refresh(self) -> None:
        if self._quitting:
            return
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
        blocked = sum(1 for project in self._projects if project.get("mode") == "isolated")
        self.projects_metric.detail.setText(
            f"{blocked} legacy isolated (blocked)"
            if blocked
            else "Local access levels · paths stay local"
        )
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
        self.account_state.setText(
            f"Signed in · {_short_id(response.get('account_id'))}"
            if response.get("account_id")
            else "Not signed in"
        )
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
        self.account_state.setText("Not connected to the local daemon")
        self._populate_projects()

    def begin_account_action(self, action: str) -> None:
        if self._quitting:
            return
        if self._account_worker is not None and self._account_worker.isRunning():
            return
        if action == "reenroll":
            answer = QMessageBox.warning(
                self,
                "Re-enroll this device?",
                "Use this only when the relay reports this device as revoked. A new device key "
                "and MCP URL will be created. Local project folders and settings are preserved, "
                "but the old ChatGPT connector must be replaced with the new URL.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif action == "logout":
            answer = QMessageBox.question(
                self,
                "Sign out and revoke device?",
                "This immediately revokes this device, its MCP link, and related OAuth grants. "
                "Project files remain unchanged.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._set_account_buttons(False)
        self.account_state.setText(
            "Waiting for browser sign-in…" if action != "logout" else "Revoking device…"
        )
        self._request({"action": "shutdown"})
        QTimer.singleShot(900, lambda selected=action: self._start_account_worker(selected))

    def _start_account_worker(self, action: str) -> None:
        if self._quitting:
            return
        worker = AccountWorker(self.config, action)
        worker.completed.connect(self._account_completed)
        worker.failed.connect(self._account_failed)
        worker.finished.connect(self._account_finished)
        self._account_worker = worker
        worker.start()

    def _account_completed(self, action: str, result: object) -> None:
        if self._quitting:
            return
        if action == "logout":
            self.account_state.setText("Signed out · device revoked")
            self._set_offline("Sign in to connect this device")
            QMessageBox.information(
                self, "Signed out", "The device, MCP link, and OAuth grants were revoked."
            )
            return
        self._start_daemon_process()
        tokens = getattr(result, "tokens", None)
        mcp_url = str(getattr(tokens, "mcp_url", "") or "")
        self.mcp_url.setText(mcp_url)
        self.account_state.setText("Signed in · starting secure connection…")
        QMessageBox.information(
            self,
            "Device enrolled" if action == "reenroll" else "Sign-in complete",
            "Codito is starting the secure relay connection.\n\n"
            + (f"New ChatGPT MCP URL:\n{mcp_url}" if mcp_url else ""),
        )
        QTimer.singleShot(1800, self.refresh)

    def _account_failed(self, message: str) -> None:
        if self._quitting:
            return
        self.account_state.setText("Account action failed")
        QMessageBox.critical(self, "Codito account error", message)

    def _account_finished(self) -> None:
        worker, self._account_worker = self._account_worker, None
        if worker is not None:
            worker.deleteLater()
        if self._quitting:
            self._finish_quit_when_idle()
        else:
            self._set_account_buttons(True)

    def _set_account_buttons(self, enabled: bool) -> None:
        self.login_button.setEnabled(enabled)
        self.reenroll_button.setEnabled(enabled)
        self.logout_button.setEnabled(enabled)

    def _start_daemon_process(self) -> None:
        if getattr(sys, "frozen", False):
            executable = (
                Path(sys.executable).resolve().parent.parent / "daemon" / "codito-agent-daemon.exe"
            )
            command = [str(executable)]
        else:
            command = [sys.executable, "-m", "codito_agent", "daemon"]
        subprocess.Popen(  # noqa: S603 - fixed, local application entry point
            command,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

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
            mode.setPlaceholderText(
                "Legacy shell trust — choose access"
                if project.get("mode") == "native_trusted"
                else "Legacy isolated (blocked) — choose access"
            )
            mode.setToolTip(PROJECT_ACCESS_HELP)
            for value, label in MODE_LABELS.items():
                mode.addItem(label, value)
            current = mode.findData(str(project.get("mode") or "isolated"))
            # No implicit upgrade of legacy isolated/unknown policies. The
            # placeholder is not a fourth selectable access mode.
            mode.setCurrentIndex(current)
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
            if (
                state == "succeeded"
                and item.get("capability") == "device_screenshot"
                and error == "outcome_unknown"
            ):
                error = "image not retained"
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
        if mode not in MODE_LABELS or project is None:
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(previous))
            combo.blockSignals(False)
            return
        if mode == previous:
            return
        acknowledged = False
        if mode in {"full_access", "native_project"}:
            answer = QMessageBox.warning(
                self,
                f"Enable {MODE_LABELS[mode]}?",
                (
                    "Project commands run without prompts. Outside working directories and "
                    "declared/detected external paths require approval. File reads, edits and "
                    "deletes inside this project do not prompt. Path detection is conservative, "
                    "not a guarantee of containment. "
                    if mode == "native_project"
                    else "Allow an authorized client using this project to read, edit and delete "
                    "any accessible files, execute Windows commands (including Docker, SSH and "
                    "network access), capture screens including private windows, and open browsers "
                    "WITHOUT LOCAL PROMPTS. OAuth scopes and locked-desktop protections "
                    "still apply. "
                )
                + "Commands have your full Windows user filesystem and network authority. "
                "This is NOT a sandbox: scripts can construct other paths and child processes "
                "can access them.\n\nEnable only for a project and account you trust.",
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
        combo.setPlaceholderText("Choose access level")
        self.statusBar().showMessage(f"Access level changed to {MODE_LABELS[mode]}", 3000)

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
            "Automatic updates enabled" if enabled else "Automatic updates disabled",
            3000,
        )

    def _check_automatic_update(self) -> None:
        if bool(self._status.get("auto_update")):
            self.check_for_updates(silent=True)

    def check_for_updates(self, *, silent: bool) -> None:
        if self._quitting:
            return
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
        if self._quitting:
            return
        if not isinstance(value, ReleaseUpdate):
            self._update_failed("GitHub returned an invalid update response")
            return
        if not value.available:
            self.update_status.setText(f"Version {__version__} · Up to date")
            if not self._update_check_silent:
                QMessageBox.information(self, "Codito updates", "Codito is up to date.")
            return
        self.update_status.setText(f"Version {value.latest_version} available")
        if self._update_check_silent:
            self._pending_update = value
            return
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
        if self._quitting:
            return
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
        self.quit_ui()

    def _update_failed(self, message: str) -> None:
        if self._quitting:
            return
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
        if self._quitting:
            self._finish_quit_when_idle()
            return
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
        if self._quitting:
            return
        self._refresh_corner_panels()
        if self._approval_dialog_active:
            return
        review = self._request({"action": "approval.review.next"})
        if review and isinstance(review.get("request_id"), str):
            self.review_pending_approvals(request_id=review["request_id"])
            return
        response = self._request(
            {"action": "approval.next", "exclude_ids": sorted(self._notified_approvals)[:32]}
        )
        if not response or not response.get("ok"):
            return
        pending_ids = response.get("pending_ids")
        if isinstance(pending_ids, list) and all(isinstance(value, str) for value in pending_ids):
            self._notified_approvals.intersection_update(pending_ids)
        approval = response.get("approval") if response else None
        if not isinstance(approval, dict):
            # None may mean every live request was already notified, not an empty
            # queue. Clearing here would repeatedly toast the same pending request.
            return
        identifier = str(approval["request_id"])
        if identifier in self._notified_approvals:
            return
        self._notified_approvals.add(identifier)
        self._notify_approval(approval)

    def review_pending_approvals(self, *, request_id: str | None = None) -> None:
        """Open details only on an explicit local review action, never on arrival."""
        if self._quitting or self._approval_dialog_active:
            return
        response = self._request({"action": "approval.next", "request_id": request_id})
        approval = response.get("approval") if response else None
        if not isinstance(approval, dict):
            self.statusBar().showMessage("No pending approval", 3000)
            return
        self._approval_dialog_active = True
        try:
            self._show_approval(approval)
        except Exception as exc:
            event(
                "approval_display_failed", str(approval.get("request_id", "")), type(exc).__name__
            )
            self.statusBar().showMessage(
                "Approval display failed. Request is retained; retry from Overview."
            )
        finally:
            self._approval_dialog_active = False

    def _notify_approval(self, approval: dict[str, Any]) -> None:
        """Submission is not display proof; retain a nonactivating local fallback."""
        if self._quitting:
            return
        self._schedule_corner_approval(approval, delay=3.0)

        def fallback_panel() -> None:
            if self._quitting:
                return
            self._schedule_corner_approval(approval, delay=0.0)
            self._refresh_corner_panels()

        def delivered(delivery: ToastDelivery) -> None:
            if not delivery.banner_expected:
                fallback_panel()

        if self._actionable_toasts and approval.get("toast_tokens"):
            worker = ApprovalToastWorker(self.config.broker_path, approval)
            self._toast_workers.append(worker)
            worker.failed.connect(fallback_panel)
            worker.delivered.connect(delivered)

            def completed() -> None:
                self._toast_workers.remove(worker)
                worker.deleteLater()
                self._finish_quit_when_idle()

            worker.finished.connect(completed)
            worker.start()
        else:
            fallback_panel()

    def _schedule_corner_approval(self, approval: dict[str, Any], *, delay: float) -> None:
        if self._quitting:
            return
        identifier = str(approval["request_id"])
        ready_at = time.monotonic() + delay
        previous = self._corner_pending.get(identifier)
        if previous is not None:
            ready_at = min(ready_at, previous[1])
        if previous is not None or len(self._corner_pending) < 32:
            self._corner_pending[identifier] = (approval, ready_at)

    def _remove_corner_panel(self, identifier: str, *, forget: bool = True) -> None:
        panel = self._corner_panels.pop(identifier, None)
        if panel is not None:
            panel.hide()
            panel.close()
            panel.deleteLater()
        if forget:
            self._corner_pending.pop(identifier, None)

    def _hide_corner_panels(self) -> None:
        for panel in self._corner_panels.values():
            panel.hide()

    def _refresh_corner_panels(self) -> None:
        from .desktop_capture import desktop_is_unlocked

        if not self._corner_pending:
            return
        for identifier, (approval, _) in list(self._corner_pending.items()):
            try:
                expired = datetime.fromisoformat(str(approval["deadline_at"])) <= datetime.now(UTC)
            except (KeyError, TypeError, ValueError):
                expired = True
            if expired:
                self._remove_corner_panel(identifier)
        if not self._corner_pending:
            return
        # One read-only queue snapshot covers all panels, including requests that
        # were answered through a native toast while Codito was in the background.
        snapshot = self._request(
            {"action": "approval.next", "exclude_ids": sorted(self._corner_pending)[:32]}
        )
        pending_ids = snapshot.get("pending_ids") if snapshot and snapshot.get("ok") else None
        if not isinstance(pending_ids, list) or not all(isinstance(v, str) for v in pending_ids):
            self._hide_corner_panels()
            return
        active_ids = set(pending_ids)
        for identifier in list(self._corner_pending):
            if identifier not in active_ids:
                self._remove_corner_panel(identifier)
        if (
            self._quitting
            or self._approval_dialog_active
            or self._corner_deciding
            or self._capture_in_progress
            or time.monotonic() < self._corner_suspended_until
            or not desktop_is_unlocked()
        ):
            self._hide_corner_panels()
            return
        screen = cast("QScreen | None", QApplication.primaryScreen())
        if screen is None:
            return
        area = screen.availableGeometry()
        bottom = area.bottom() - 12
        visible_count = 0
        visible_ids: set[str] = set()
        for identifier, (approval, ready_at) in self._corner_pending.items():
            if identifier in self._corner_deciding or ready_at > time.monotonic():
                continue
            if visible_count >= 3:
                break
            panel = self._corner_panels.get(identifier)
            if panel is None:
                panel = ApprovalCornerPanel(approval)
                panel.decision_chosen.connect(
                    lambda decision, request_id=identifier: self._corner_decision(
                        request_id, decision
                    )
                )
                panel.review_requested.connect(
                    lambda request_id=identifier: self._corner_review(request_id)
                )
                self._corner_panels[identifier] = panel
            panel.adjustSize()
            if bottom - panel.height() < area.top() and visible_count:
                panel.hide()
                break
            panel.move(max(area.left(), area.right() - panel.width() - 12), bottom - panel.height())
            was_visible = panel.isVisible()
            panel.show()  # WA_ShowWithoutActivating; never raise/activate the main window.
            if not was_visible and panel.isVisible():
                self._request({"action": "approval.displayed", "request_id": identifier})
                event("approval_corner_displayed", identifier)
            bottom -= panel.height() + 12
            visible_count += 1
            visible_ids.add(identifier)
        for identifier, panel in self._corner_panels.items():
            if identifier not in visible_ids:
                panel.hide()

    def _corner_decision(self, identifier: str, decision: str) -> None:
        if self._quitting:
            return
        cached = self._corner_pending.get(identifier)
        if cached is None or identifier in self._corner_deciding:
            return
        approval = cached[0]
        tokens = approval.get("toast_tokens", {})
        token = tokens.get(decision) if isinstance(tokens, dict) else None
        if not isinstance(token, str):
            return
        self._corner_deciding.add(identifier)
        self._hide_corner_panels()
        self._corner_suspended_until = time.monotonic() + 3.0
        self._remove_corner_panel(identifier, forget=False)

        def send_decision() -> None:
            from .desktop_capture import desktop_is_unlocked

            self._corner_deciding.discard(identifier)
            if self._quitting or not desktop_is_unlocked():
                return
            try:
                expired = datetime.fromisoformat(str(approval["deadline_at"])) <= datetime.now(UTC)
            except (KeyError, TypeError, ValueError):
                expired = True
            if expired:
                self._remove_corner_panel(identifier)
                return
            self._corner_suspended_until = time.monotonic() + 1.0
            response = self._request(
                {
                    "action": "approval.toast",
                    "request_id": identifier,
                    "decision": decision,
                    "token": token,
                }
            )
            self._remove_corner_panel(identifier)
            if not response or not response.get("ok"):
                # Fetch current pending state/tokens on the next poll; never
                # retry the same one-use authorization token automatically.
                self._notified_approvals.discard(identifier)
                event("approval_corner_decision_rejected", identifier)

        worker = ApprovalDismissWorker(self.config.broker_path, identifier)
        self._toast_workers.append(worker)

        def dismissed() -> None:
            self._toast_workers.remove(worker)
            worker.deleteLater()
            if self._quitting:
                self._corner_deciding.discard(identifier)
                self._finish_quit_when_idle()
                return
            # Let the desktop compositor present the hidden panels before the
            # daemon can enqueue a screenshot. No GUI-thread blocking sleep.
            QTimer.singleShot(200, send_decision)

        worker.finished.connect(dismissed)
        worker.start()

    def _corner_review(self, identifier: str) -> None:
        self._hide_corner_panels()
        self.review_pending_approvals(request_id=identifier)

    def _show_approval(self, approval: dict[str, Any]) -> None:
        if self._quitting:
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        dialog = QMessageBox(self)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Codito local approval")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(str(approval.get("summary", "Remote operation")))
        dialog.setInformativeText(
            f"Project: {approval.get('project_title')}\n"
            f"Capability: {approval.get('capability')}\n"
            f"Risk: {approval.get('risk')}\n\n"
            "Review the exact operation details before allowing it."
            + (
                "\n\nAlways allow shell saves native execution for this project and requested "
                "scope/account/link. It grants full Windows user authority, not confinement to "
                "these paths. A different requested scope asks again. Revoke in Settings."
                if approval.get("persistent_shell_eligible")
                else ""
            )
            + (
                "\n\nAlways allow screen saves capture access to THIS monitor for this "
                "account/connection, including private visible windows. Other monitors and "
                "layout changes require fresh approval. Revoke in Settings. It never allows "
                "file access, commands or browser control. Codito minimizes before capture."
                if approval.get("persistent_screen_eligible")
                else ""
            )
            + (
                "\n\nAlways allow saves READ access to the shown directory and all descendants. "
                "Private files and secrets there could be sent to the requesting client. "
                "It does NOT permit edits or shell execution. Revoke in Settings."
                if approval.get("persistent_read_eligible")
                else ""
            )
        )
        details = {
            "account": approval.get("account_id"),
            "oauth_grant": approval.get("grant_id"),
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
        dialog.setDetailedText(json.dumps(details, indent=2, ensure_ascii=False, default=str))
        # QMessageBox.setDetailedText rebuilds its window flags; set this afterwards.
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        deny = dialog.addButton("Deny", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(deny)
        dialog.setEscapeButton(deny)
        once = dialog.addButton("Allow once", QMessageBox.ButtonRole.AcceptRole)
        always_read = None
        always_shell = None
        always_screen = None
        if approval.get("persistent_screen_eligible"):
            always_screen = dialog.addButton(
                "Always allow this monitor", QMessageBox.ButtonRole.YesRole
            )
        if approval.get("persistent_shell_eligible"):
            always_shell = dialog.addButton(
                "Always allow shell for this scope", QMessageBox.ButtonRole.YesRole
            )
        if approval.get("persistent_read_eligible"):
            always_read = dialog.addButton(
                "Always allow reading this folder", QMessageBox.ButtonRole.YesRole
            )
        session = None
        if approval.get("session_eligible"):
            session = dialog.addButton(
                "Allow similar access for this session", QMessageBox.ButtonRole.YesRole
            )

        # Mark delivery after Qt has shown the dialog, not when IPC fetched it.
        def displayed() -> None:
            if dialog.isVisible():
                self._request(
                    {"action": "approval.displayed", "request_id": approval["request_id"]}
                )
                QApplication.alert(dialog, 10_000)
                dialog.raise_()
                dialog.activateWindow()

        # Close stale dialogs: they must never offer an actionable expired Allow.
        deadline = datetime.fromisoformat(str(approval["deadline_at"]))
        remaining = max(0, int((deadline - datetime.now(UTC)).total_seconds() * 1000))
        expiry = QTimer(dialog)
        expiry.setSingleShot(True)
        expiry.timeout.connect(dialog.reject)
        expiry.start(min(remaining, 2_147_483_647))
        resolved_elsewhere = False

        def check_resolved() -> None:
            nonlocal resolved_elsewhere
            response = self._request(
                {"action": "approval.pending", "request_id": approval["request_id"]}
            )
            if response and response.get("ok") and response.get("pending") is False:
                resolved_elsewhere = True
                dialog.reject()  # A toast decision/expiry already resolved this request.

        decision_timer = QTimer(dialog)
        decision_timer.timeout.connect(check_resolved)
        decision_timer.start(500)
        delivery_timer = QTimer(dialog)
        delivery_timer.setSingleShot(True)
        delivery_timer.timeout.connect(displayed)
        delivery_timer.start(0)
        dialog.exec()
        decision_timer.stop()
        delivery_timer.stop()
        expiry.stop()
        clicked = dialog.clickedButton()
        if self._quitting or resolved_elsewhere:
            self.refresh()
            dialog.deleteLater()
            return
        decision = "deny"
        if clicked is once:
            decision = "allow_once"
        elif session is not None and clicked is session:
            decision = "allow_session"
        elif always_read is not None and clicked is always_read:
            decision = "allow_always_read"
        elif always_shell is not None and clicked is always_shell:
            decision = "allow_always_shell"
        elif always_screen is not None and clicked is always_screen:
            decision = "allow_always_screen"
        if decision != "deny" and approval.get("capability") in {"screen:read", "desktop:open_url"}:
            # Review was explicitly opened: remove Codito before accepting the
            # request so the next GUI tick sees the intended desktop/browser.
            self.showMinimized()
        response = self._request(
            {
                "action": "approval.respond",
                "request_id": approval["request_id"],
                "decision": decision,
            }
        )
        if not response or not response.get("ok"):
            self.statusBar().showMessage(
                "Decision was not accepted (expired or disconnected). Refresh pending approvals.",
                10000,
            )
        self.refresh()
        dialog.deleteLater()

    def poll_capture(self) -> None:
        if self._quitting or self._approval_dialog_active or self._capture_in_progress:
            return
        response = self._request({"action": "screen.next"})
        request = response.get("capture") if response else None
        if not isinstance(request, dict):
            return
        self._capture_in_progress = True
        if request.get("action") != "list_displays" and any(
            panel.isVisible() for panel in self._corner_panels.values()
        ):
            self._hide_corner_panels()
            self._corner_suspended_until = time.monotonic() + 3.0
            # screen.next claims once. Retain this exact authorized request while
            # allowing the compositor to remove Codito's panels before capture.
            QTimer.singleShot(200, lambda: self._complete_capture(request))
            return
        self._complete_capture(request)

    def _complete_capture(self, request: dict[str, Any]) -> None:
        try:
            from .desktop_capture import capture_screen, list_displays

            if self._quitting or self._approval_dialog_active:
                raise ValueError("Desktop no longer available for capture")
            if datetime.fromisoformat(request["deadline_at"]) <= datetime.now(UTC):
                raise ValueError("Capture expired")
            if request.get("action") == "list_displays":
                result = list_displays()
            else:
                result = capture_screen(
                    str(request["display_id"]),
                    int(request["max_dimension"]),
                    str(request["topology_id"]),
                )
                event("screen_captured", str(request["capture_id"]))
        except Exception as exc:
            event("screen_capture_failed", str(request.get("capture_id", "")), type(exc).__name__)
            result = {"ok": False}
        try:
            self._request(
                {"action": "screen.respond", "capture_id": request["capture_id"], "result": result}
            )
        finally:
            self._capture_in_progress = False
            self._finish_quit_when_idle()

    def poll_desktop_action(self) -> None:
        if self._quitting or self._approval_dialog_active:
            return
        response = self._request({"action": "desktop.next"})
        request = response.get("desktop_action") if response else None
        if not isinstance(request, dict):
            return
        try:
            from codito_protocol.desktop_action import DeviceDesktopInput

            from .desktop_browser import open_browser_url

            if datetime.fromisoformat(request["deadline_at"]) <= datetime.now(UTC):
                raise ValueError("Desktop action expired")
            browser_request = DeviceDesktopInput(
                browser=request["browser"], url=request["url"], purpose="Approved browser request"
            )
            accepted = open_browser_url(browser_request.browser, browser_request.url)
            event(
                "desktop_action_submitted" if accepted else "desktop_action_refused",
                str(request["desktop_action_id"]),
            )
        except Exception as exc:
            event("desktop_action_failed", str(request["desktop_action_id"]), type(exc).__name__)
            accepted = False
        self._request(
            {
                "action": "desktop.respond",
                "desktop_action_id": request["desktop_action_id"],
                "result": {"ok": accepted},
            }
        )

    def review_screen_permissions(self) -> None:
        self._review_permissions("screen")

    def review_read_permissions(self) -> None:
        self._review_permissions("read")

    def review_shell_permissions(self) -> None:
        self._review_permissions("shell")

    def _review_permissions(self, category: PermissionCategory) -> None:
        if self._quitting:
            return
        dialog = PermissionReviewDialog(category, self._request, self)
        dialog.exec()
        dialog.deleteLater()
        self.refresh()

    def poll_project_request(self) -> None:
        if self._quitting or self._approval_dialog_active:
            return
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
        if self._quitting:
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.refresh()

    def quit_ui(self) -> None:
        self._quitting = True
        self._pending_update = None
        for timer in (
            self.refresh_timer,
            self.approval_timer,
            self.capture_timer,
            self.project_request_timer,
            self.update_timer,
        ):
            timer.stop()
        for identifier in list(self._corner_pending):
            self._remove_corner_panel(identifier)
        self.tray.hide()
        self.hide()
        self._finish_quit_when_idle()

    def _finish_quit_when_idle(self) -> None:
        # Keep the event loop/owners alive until bounded broker subprocesses
        # return and their QThreads emit finished. Never terminate a QThread.
        if (
            not self._quitting
            or self._quit_requested_to_app
            or self._toast_workers
            or self._account_worker is not None
            or self._update_worker is not None
            or self._capture_in_progress
        ):
            return
        self._quit_requested_to_app = True
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            self.quit_ui()
            if self._quit_requested_to_app:
                event.accept()
            else:
                event.ignore()
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
    return int(app.exec())
