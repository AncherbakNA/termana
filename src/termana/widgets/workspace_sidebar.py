"""Connections/files switcher and a transfer queue independent of the active tab."""

import posixpath

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from termana.dialogs.action_dialog import ActionDialog
from termana.i18n import tr
from termana.models.sftp import SftpOperation, SftpTransferUpdate
from termana.qt_lifecycle import schedule_delete_later
from termana.theme import DEFAULT_THEME, ThemeColors
from termana.ui_icons import interface_icon
from termana.ui_text import plain_text_tooltip
from termana.ui_toolbar import workbench_icon_colors
from termana.ui_typography import scale_widget_font
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.chrome_tool_button import ChromeToolButton, anchor_tooltip
from termana.widgets.clipboard_text import copy_plain_text
from termana.widgets.command_snippets_sidebar import CommandSnippetsSidebar
from termana.widgets.sftp_browser import MAX_JOB_HISTORY, file_label
from termana.widgets.wrapping_button import WrappingPushButton
from termana.widgets.wrapping_label import WrappingLabel


class SftpTransferQueue(QWidget):
    """Bounded shared history; cancellation carries the original backend and request."""

    cancel_requested = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = {}
        self._rows = {}
        self._manual_expansion = None
        self._setting_expansion = False
        self._bulk_dialog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 8)
        self.toggle = QToolButton()
        self.toggle.setText(tr("File jobs"))
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow)
        layout.addWidget(self.toggle)
        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self.status_filter = QComboBox()
        self.status_filter.setAccessibleName(tr("File job status filter"))
        for label, value in (
            (tr("All jobs"), "all"),
            (tr("Active"), "active"),
            (tr("Failed"), "failed"),
            (tr("Finished"), "finished"),
        ):
            self.status_filter.addItem(label, value)
        self.owner_filter = QComboBox()
        self.owner_filter.setAccessibleName(tr("File job SSH owner filter"))
        self.owner_filter.addItem(tr("All SSH sessions"), None)
        for selector in (self.status_filter, self.owner_filter):
            selector.setMinimumContentsLength(8)
            selector.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
        content_layout.addWidget(self.status_filter)
        content_layout.addWidget(self.owner_filter)
        self.counts = WrappingLabel("")
        self.counts.setTextFormat(Qt.TextFormat.PlainText)
        content_layout.addWidget(self.counts)
        self.jobs = QTableWidget(0, 2)
        self.jobs.setAccessibleName(tr("File jobs from all SSH sessions"))
        self.jobs.setHorizontalHeaderLabels([tr("File job / SSH owner"), tr("Status")])
        self.jobs.verticalHeader().hide()
        self.jobs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.jobs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs.setMinimumWidth(0)
        self.jobs.setMaximumHeight(170)
        content_layout.addWidget(self.jobs)
        self.details = WrappingLabel(tr("Select a file job to view its owner and status."))
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        content_layout.addWidget(self.details)
        self.cancel_button = WrappingPushButton(tr("Cancel job"))
        self.cancel_button.setEnabled(False)
        self.copy_button = WrappingPushButton(tr("Copy details"))
        self.copy_button.setEnabled(False)
        self.clear_button = WrappingPushButton(tr("Clear finished"))
        self.cancel_visible_button = WrappingPushButton(tr("Cancel visible active…"))
        content_layout.addWidget(AdaptiveButtonRow((self.cancel_button, self.copy_button)))
        content_layout.addWidget(AdaptiveButtonRow((self.clear_button, self.cancel_visible_button)))
        layout.addWidget(self.content)
        for widget in (
            self.toggle,
            self.jobs,
            self.details,
            self.cancel_button,
            self.copy_button,
            self.clear_button,
            self.cancel_visible_button,
            self.status_filter,
            self.owner_filter,
            self.counts,
        ):
            scale_widget_font(widget, 1.0)
        self.toggle.toggled.connect(self._set_expanded)
        self.jobs.itemSelectionChanged.connect(self._selection_changed)
        self.cancel_button.clicked.connect(self._cancel)
        self.copy_button.clicked.connect(self._copy_details)
        self.clear_button.clicked.connect(self.clear_finished)
        self.cancel_visible_button.clicked.connect(self._cancel_visible)
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        self.owner_filter.currentIndexChanged.connect(self._apply_filters)
        self._apply_expanded(False)
        self.hide()

    def _set_expanded(self, expanded):
        if not self._setting_expansion:
            self._manual_expansion = expanded
        self.content.setVisible(expanded)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _apply_expanded(self, expanded):
        self._setting_expansion = True
        try:
            self.toggle.setChecked(expanded)
            self._set_expanded(expanded)
        finally:
            self._setting_expansion = False

    def update_transfer(self, backend, owner, transfer: SftpTransferUpdate):
        key = (backend, transfer.request.request_id)
        created = key not in self._rows
        if created:
            # Keep pending jobs, but cap completed history across all owners.
            while len(self._rows) >= MAX_JOB_HISTORY:
                candidate = next(
                    (key for key, (_, item) in self._entries.items() if not item.pending), None
                )
                if candidate is None:
                    break
                self._remove_entry(candidate)
            row = self.jobs.rowCount()
            self.jobs.insertRow(row)
            self._rows[key] = row
            item = QTableWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.jobs.setItem(row, 0, item)
            self.jobs.setItem(row, 1, QTableWidgetItem())
        self._entries[key] = (file_label(owner), transfer)
        row = self._rows[key]
        action = tr(transfer.request.operation.label)
        label = f"{action}: {file_label(posixpath.basename(transfer.request.remote_path))}"
        self.jobs.item(row, 0).setText(f"{label}\n{file_label(owner)}")
        self.jobs.item(row, 0).setToolTip(
            plain_text_tooltip(f"{file_label(owner)}\n{file_label(transfer.request.remote_path)}")
        )
        status_item = self.jobs.item(row, 1)
        changed_status = status_item.text() != transfer.status
        if changed_status:
            status_item.setText(transfer.status)
            status_item.setToolTip(plain_text_tooltip(transfer.status))
        if created or changed_status:
            self.jobs.resizeRowToContents(row)
        # Active jobs across owners may temporarily exceed the history cap.
        # Trim again when existing rows finish, never discarding pending jobs.
        while len(self._entries) > MAX_JOB_HISTORY:
            candidate = next((k for k, (_, t) in self._entries.items() if not t.pending), None)
            if candidate is None:
                break
            self._remove_entry(candidate)
        pending = sum(item.pending for _, item in self._entries.values())
        self.toggle.setText(tr("File jobs · {p0} active", p0=f"{pending}"))
        if self._manual_expansion is None:
            focus = QApplication.focusWidget()
            focused_content = focus is self.content or (
                focus is not None and self.content.isAncestorOf(focus)
            )
            if not pending and focused_content:
                self._manual_expansion = True
            else:
                self._apply_expanded(pending > 0)
        self.show()
        self._refresh_owners()
        self._apply_filters()

    def _remove_entry(self, key):
        selected = self._selected_key() == key
        row = self._rows.pop(key)
        self._entries.pop(key)
        self.jobs.removeRow(row)
        if selected:
            # Qt otherwise selects the neighbouring row after removing the current one.
            self.jobs.clearSelection()
            self.jobs.setCurrentCell(-1, -1)
        self._rows = {
            entry: (index - 1 if index > row else index) for entry, index in self._rows.items()
        }

    def _refresh_owners(self):
        selected = self.owner_filter.currentData()
        owners = {}
        for (backend, _), (label, _) in self._entries.items():
            owners.setdefault(backend, label)
        with QSignalBlocker(self.owner_filter):
            self.owner_filter.clear()
            self.owner_filter.addItem(tr("All SSH sessions"), None)
            for number, (backend, label) in enumerate(owners.items(), 1):
                # Number distinguishes equal display labels; routing uses identity, never text.
                self.owner_filter.addItem(f"{number}. {label}", backend)
                if backend is selected:
                    self.owner_filter.setCurrentIndex(number)

    def _matches(self, key, transfer):
        owner = self.owner_filter.currentData()
        if owner is not None and key[0] is not owner:
            return False
        mode = self.status_filter.currentData()
        return (
            mode == "all"
            or (mode == "active" and transfer.pending)
            or (mode == "failed" and not transfer.pending and transfer.result_status == "error")
            or (mode == "finished" and not transfer.pending)
        )

    def _apply_filters(self, *_):
        selected = self._selected_key()
        visible = 0
        for key, (_, transfer) in self._entries.items():
            shown = self._matches(key, transfer)
            visible += shown
            self.jobs.setRowHidden(self._rows[key], not shown)
        if selected is not None and self.jobs.isRowHidden(self._rows[selected]):
            self.jobs.clearSelection()
            self.jobs.setCurrentCell(-1, -1)
        active = sum(transfer.pending for _, transfer in self._entries.values())
        failed = sum(
            not t.pending and t.result_status == "error" for _, t in self._entries.values()
        )
        finished = len(self._entries) - active
        self.counts.setText(
            tr(
                "{p0} shown · {p1} active · {p2} failed · {p3} finished retained",
                p0=f"{visible}",
                p1=f"{active}",
                p2=f"{failed}",
                p3=f"{finished}",
            )
        )
        self.clear_button.setEnabled(finished > 0)
        self.cancel_visible_button.setEnabled(
            any(t.pending and self._matches(key, t) for key, (_, t) in self._entries.items())
        )
        self._selection_changed()

    def clear_finished(self):
        """Clear local completed history only; never cancel or touch remote files."""
        for key, (_, transfer) in tuple(self._entries.items()):
            if not transfer.pending:
                self._remove_entry(key)
        self._refresh_owners()
        self._apply_filters()

    def _selected_key(self):
        item = self.jobs.item(self.jobs.currentRow(), 0)
        if item is None or not item.isSelected() or self.jobs.isRowHidden(item.row()):
            return None
        key = item.data(Qt.ItemDataRole.UserRole)
        return key if key in self._entries else None

    @staticmethod
    def _entry_details(entry):
        owner, transfer = entry
        total = str(transfer.total) if transfer.total is not None else "?"
        progress = (
            ""
            if transfer.request.operation is SftpOperation.MKDIR
            else tr(" · {p0} / {p1} bytes", p0=f"{transfer.transferred}", p1=f"{total}")
        )
        return f"{owner}\n{file_label(transfer.request.remote_path)}\n{file_label(transfer.status)}{progress}"

    def _selection_changed(self):
        entry = self._entries.get(self._selected_key())
        self.cancel_button.setEnabled(entry is not None and entry[1].pending)
        self.copy_button.setEnabled(entry is not None)
        self.details.setText(
            self._entry_details(entry)
            if entry is not None
            else tr("Select a file job to view its owner and status.")
        )

    def _cancel(self):
        key = self._selected_key()
        if key in self._entries and self._entries[key][1].pending:
            self.cancel_requested.emit(*key)

    def _copy_details(self):
        entry = self._entries.get(self._selected_key())
        if entry is None:
            return
        if copy_plain_text(self._entry_details(entry)):
            self.details.setText(
                tr("Job details copied. This is local queue history, not server state.")
            )
        else:
            self.details.setText(tr("Clipboard unavailable. Select the job to view details again."))

    def _cancel_visible(self):
        if self._bulk_dialog is not None:
            return
        approved = tuple(
            key for key, (_, t) in self._entries.items() if t.pending and self._matches(key, t)
        )
        if not approved:
            return
        dialog = ActionDialog(
            object_name="cancelVisibleFileJobsDialog",
            window_title=tr("Cancel file jobs"),
            title=tr("Cancel {p0} visible active jobs?", p0=f"{len(approved)}"),
            subtitle=tr("File job queue"),
            body=tr(
                "Only these queued/running jobs are requested to stop. Completed files are not removed; inspect partial destinations."
            ),
            primary_text=tr("Cancel jobs"),
            primary_object_name="confirmCancelFileJobs",
            secondary_text=tr("Keep jobs"),
            secondary_object_name="keepFileJobs",
            primary_role="danger",
            parent=self,
        )
        self._bulk_dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._bulk_dialog = None
            if isValid(dialog):
                schedule_delete_later(dialog)
        if accepted and isValid(self):
            for key in approved:
                if not isValid(self):
                    break
                entry = self._entries.get(key)
                if entry is not None and entry[1].pending and self._matches(key, entry[1]):
                    self.cancel_requested.emit(*key)


class WorkspaceSidebar(QWidget):
    """Keep catalog, commands and owner-bound files one explicit click apart."""

    files_requested = Signal()
    commands_requested = Signal()

    def __init__(self, connections, parent=None):
        super().__init__(parent)
        self.setObjectName("workspaceSidebar")
        self.setMinimumWidth(230)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.connections = connections
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 0, 0)
        layout.setSpacing(0)
        rail = QVBoxLayout()
        rail.setContentsMargins(0, 0, 4, 4)
        rail.setSpacing(4)
        self.connections_button = ChromeToolButton()
        self.commands_button = ChromeToolButton()
        self.files_button = ChromeToolButton()
        self.files_button.setEnabled(False)
        self._buttons = QButtonGroup(self)
        for button, label in (
            (self.connections_button, tr("Connections")),
            (self.commands_button, tr("Commands")),
            (self.files_button, tr("Files")),
        ):
            button.setText(label)
            button.setToolTip(label)
            anchor_tooltip(button, placement="right")
            button.setAccessibleName(label)
            button.setCheckable(True)
            button.setProperty("sidebarSurface", True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(22, 22))
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self._buttons.addButton(button)
            rail.addWidget(button)
        rail.addStretch(1)
        layout.addLayout(rail)
        self.rail_separator = QFrame(self)
        self.rail_separator.setObjectName("sidebarRailSeparator")
        self.rail_separator.setFixedWidth(1)
        self.rail_separator.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.rail_separator.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rail_separator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.rail_separator)
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(content, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(connections)
        self.commands = CommandSnippetsSidebar()
        self.stack.addWidget(self.commands)
        self.file_stack = QStackedWidget()
        self.placeholder = WrappingLabel(tr("Select a running SSH session to browse files."))
        self.placeholder.setTextFormat(Qt.TextFormat.PlainText)
        self.file_stack.addWidget(self.placeholder)
        self.stack.addWidget(self.file_stack)
        content.addWidget(self.stack, 1)
        self.transfers = SftpTransferQueue(self)
        content.addWidget(self.transfers)
        self.connections_button.clicked.connect(self.show_connections)
        self.commands_button.clicked.connect(self.commands_requested)
        self.files_button.clicked.connect(self.files_requested)
        self.show_connections()
        self.set_interface_colors(DEFAULT_THEME)

    @property
    def showing_files(self):
        return self.stack.currentWidget() is self.file_stack

    def show_connections(self):
        self.connections_button.setChecked(True)
        self.stack.setCurrentWidget(self.connections)

    def show_commands(self):
        self.commands_button.setChecked(True)
        self.stack.setCurrentWidget(self.commands)

    def show_files(self, browser):
        if self.file_stack.indexOf(browser) < 0:
            self.file_stack.addWidget(browser)
        self.file_stack.setCurrentWidget(browser)
        self.stack.setCurrentWidget(self.file_stack)
        self.files_button.setChecked(True)

    def remove_browser(self, browser):
        if self.file_stack.currentWidget() is browser:
            if self.showing_files:
                self.show_connections()
            self.file_stack.setCurrentWidget(self.placeholder)
        self.file_stack.removeWidget(browser)

    def set_files_available(self, available, *, explanation=None):
        self.files_button.setEnabled(available)
        self.files_button.setToolTip(
            explanation
            or tr(
                "Remote files for the selected SSH session. System OpenSSH may ask for a separate connection."
            )
        )
        self.files_button.setAccessibleDescription(self.files_button.toolTip())
        if not available:
            if self.showing_files:
                self.show_connections()
            self.file_stack.setCurrentWidget(self.placeholder)

    def set_interface_colors(self, colors: ThemeColors, *, monochrome=False):
        self.connections.set_interface_colors(colors, monochrome=monochrome)
        tones = workbench_icon_colors(colors, monochrome=monochrome)
        self.connections_button.setIcon(interface_icon("server", colors.text))
        self.commands_button.setIcon(interface_icon("terminal", tones["command"]))
        self.files_button.setIcon(interface_icon("folder", tones["folder"]))
