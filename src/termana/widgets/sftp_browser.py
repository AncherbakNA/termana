"""An embeddable file browser owned by one authenticated SSH session."""

import posixpath
from collections import deque
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QItemSelectionModel, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from termana.i18n import count_noun, tr
from termana.models.sftp import (
    MAX_SFTP_PATH_CHARACTERS,
    MAX_SFTP_PENDING,
    SftpOperation,
    SftpRequest,
    SftpTransferUpdate,
    SftpTreePolicy,
)
from termana.models.sftp_batch import (
    MAX_SFTP_BATCH_CONTEXTS,
    SftpBatchContext,
    download_batch,
    upload_batch,
    upload_selection,
)
from termana.sftp_presentation import sftp_result_text
from termana.theme import DEFAULT_THEME, ThemeColors
from termana.ui_icons import interface_icon, selection_interface_icon
from termana.ui_text import plain_text_tooltip
from termana.ui_toolbar import workbench_icon_colors
from termana.ui_typography import scale_widget_font
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.clipboard_text import copy_plain_text
from termana.widgets.eliding_label import ElidingLabel
from termana.widgets.sftp_browser_tools import SftpBrowserTools
from termana.widgets.sftp_drop_table import SftpDropTable
from termana.widgets.sftp_file_tools import SftpFileTools
from termana.widgets.sftp_folder_transfers import SftpFolderTransfers
from termana.widgets.sftp_places import SftpPlaces
from termana.widgets.sftp_tools_dialog import SftpToolsDialog
from termana.widgets.themed_checkbox import ThemedCheckBox
from termana.widgets.wrapping_button import WrappingPushButton
from termana.widgets.wrapping_label import WrappingLabel

MAX_JOB_HISTORY = 100
MAX_DIRECTORY_HISTORY = 50
FILE_COLUMN_ALIGNMENTS = (
    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
)


def file_label(text):
    """Keep names single-line and non-directional without changing path identity."""
    return "".join(
        f"\\u{ord(char):04x}"
        if ord(char) < 32
        or ord(char) == 127
        or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        else char
        for char in text
    )


def _table(headers):
    table = SftpDropTable(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return table


class SftpBrowser(QWidget):
    transfer_changed = Signal(object)

    def __init__(self, parent, backend, title):
        super().__init__(parent)
        self._backend = backend
        self._closed = False
        self._session_ended = False
        self._overwrite_dialog = None
        self._folder_dialog = None
        self._directory = None
        self._listing = None
        self._listing_history_index = None
        self._directory_history = []
        self._history_index = -1
        self._pending = {}
        self._transfers = {}
        self._batches = {}
        self._request_batches = {}
        self._conflicts = deque()
        self._conflict_active = False
        self._items = []
        self._parent_row_count = 0
        self._loaded_items = []
        self._listing_generation = 0
        self._filter_names = []
        self._listing_truncated = False
        self._omitted_entries = 0
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        self.body_scroll = QScrollArea(self)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.body_scroll.setWidget(body)
        root_layout.addWidget(self.body_scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        self.owner_label = ElidingLabel(file_label(title))
        self.owner_label.setObjectName("sftpOwnerLabel")
        self.owner_label.setTextFormat(Qt.TextFormat.PlainText)
        self.owner_label.setAccessibleName(tr("File browser SSH owner"))
        layout.addWidget(self.owner_label)
        connection_note = getattr(backend, "connection_note", None)
        separate_connection = connection_note is not None
        if connection_note == "SFTP · Separate System OpenSSH connection for this tab":
            connection_note = tr("SFTP · Separate System OpenSSH connection for this tab")
        elif connection_note is None:
            connection_note = tr("SFTP · Same verified SSH connection")
        note = WrappingLabel(connection_note)
        note.setObjectName("sftpConnectionNote")
        note.setWordWrap(True)
        self.owner_label.setToolTip(plain_text_tooltip(file_label(title) + "\n" + connection_note))
        self.owner_label.setAccessibleDescription(file_label(title) + "\n" + connection_note)
        self.connection_badge = ElidingLabel(tr("Separate SFTP connection"))
        self.connection_badge.setObjectName("sftpConnectionBadge")
        self.connection_badge.setToolTip(plain_text_tooltip(connection_note))
        self.connection_badge.setAccessibleDescription(connection_note)
        self.connection_badge.setVisible(separate_connection)
        layout.addWidget(self.connection_badge)
        self.tools_dialog = SftpToolsDialog(self, note)
        details_layout = self.tools_dialog.page_layouts["display"]
        self.path_input = QLineEdit()
        self.path_input.setAccessibleName(tr("Remote directory"))
        self.path_input.setPlaceholderText(tr("Remote directory"))
        self.path_input.setMaxLength(MAX_SFTP_PATH_CHARACTERS)
        layout.addWidget(self.path_input)
        self.back_button = QToolButton()
        self.forward_button = QToolButton()
        self.up_button = QToolButton()
        # Retain the existing Alt+Up action, not another visible toolbar control.
        self.up_button.setParent(self)
        self.up_button.hide()
        self.refresh_button = QToolButton()
        for button, label in (
            (self.back_button, tr("Back")),
            (self.forward_button, tr("Forward")),
            (self.up_button, tr("Up")),
            (self.refresh_button, tr("Refresh")),
        ):
            button.setText(label)
            button.setAccessibleName(label + tr(" remote directory"))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(18, 18))
            button.setAutoRaise(True)
            button.setProperty("navigationRole", "remoteDirectory")
        self.back_button.setAccessibleName(tr("Previous remote directory"))
        self.forward_button.setAccessibleName(tr("Next remote directory"))
        self.back_button.setToolTip(tr("Previous directory in this SSH session (Alt+Left)"))
        self.forward_button.setToolTip(tr("Next directory in this SSH session (Alt+Right)"))
        self.up_button.setToolTip(tr("Parent directory (Alt+Up)"))
        self.refresh_button.setToolTip(tr("Refresh this directory (F5)"))
        self.tools_dialog.page_layouts["actions"].addWidget(
            AdaptiveButtonRow((self.back_button, self.forward_button))
        )
        self._places = SftpPlaces(self)
        self.tools_dialog.page_layouts["places"].addWidget(self._places)
        self.tools_dialog.page_layouts["places"].addStretch()
        self.filter_input = QLineEdit()
        self.filter_input.setObjectName("remoteFileFilter")
        self.filter_input.setAccessibleName(tr("Filter loaded remote files"))
        self.filter_input.setPlaceholderText(tr("Filter loaded files"))
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.setMaxLength(MAX_SFTP_PATH_CHARACTERS)
        self.filter_input.setToolTip(
            tr("Filter only the loaded directory by name, without sending another SSH request.")
        )
        details_layout.addWidget(self.filter_input)
        self.listing_summary = ElidingLabel(tr("No directory loaded"))
        self.listing_summary.setObjectName("sftpListingSummary")
        self.listing_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.listing_summary.setAccessibleName(tr("Remote file filter results"))
        self.listing_summary.setToolTip(
            tr(
                "Listings have record and total-name-size limits. Enter a subdirectory path to browse further."
            )
        )
        layout.addWidget(self.listing_summary)
        self.files = _table([tr("Name"), tr("Bytes")])
        self.files.setShowGrid(False)
        for column, alignment in enumerate(FILE_COLUMN_ALIGNMENTS):
            self.files.horizontalHeaderItem(column).setTextAlignment(alignment)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.drop_available = lambda: (
            self._directory_ready(self._directory)
            and not self._conflict_active
            and self._overwrite_dialog is None
            and self._folder_dialog is None
        )
        self.files.files_dropped.connect(self._upload_drop)
        self.files.setAccessibleName(tr("Remote files"))
        self.files.setToolTip(
            tr(
                "Double-click a folder or press Enter to open it. Files are never downloaded automatically."
            )
        )
        self.files.setMinimumWidth(0)
        self.files.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.files, 3)
        self.upload_button = QToolButton()
        self.upload_button.setText(tr("Upload…"))
        self.download_button = QToolButton()
        self.download_button.setText(tr("Download…"))
        self.upload_many_button = WrappingPushButton(tr("Upload files…"))
        self.upload_folder_button = WrappingPushButton(tr("Upload folder…"))
        self.upload_folder_button.setAccessibleName(tr("Upload folder including subfolders"))
        self.upload_folder_button.setToolTip(
            tr("Copy a folder tree, including empty folders. Links are excluded, never followed.")
        )
        self._folder_transfers = SftpFolderTransfers(self)
        self.upload_many_button.setToolTip(tr("Select up to 64 regular files for this SSH folder."))
        self.secondary_actions_button = QToolButton()
        self.secondary_actions_button.setText(tr("More file actions"))
        self.secondary_actions_button.setAccessibleName(tr("More remote file actions"))
        self.secondary_actions_button.setCheckable(True)
        self.secondary_actions_button.setAutoRaise(True)
        self.secondary_actions_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.secondary_actions_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.secondary_actions_button.setProperty("navigationRole", "remoteDirectory")
        scale_widget_font(self.secondary_actions_button, 1.0)
        self.secondary_actions = QWidget()
        self.secondary_actions_layout = QVBoxLayout(self.secondary_actions)
        self.secondary_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.secondary_actions_layout.addWidget(
            AdaptiveButtonRow((self.upload_many_button, self.upload_folder_button))
        )
        self.tools_dialog.page_layouts["actions"].addWidget(self.secondary_actions)
        self.tools_dialog.finished.connect(self._file_tools_closed)
        self.secondary_actions_button.toggled.connect(self._toggle_secondary_actions)
        self.copy_path_button = WrappingPushButton(tr("Copy path"))
        self.copy_path_button.setObjectName("copyRemoteFilePath")
        self.copy_path_button.setAccessibleName(tr("Copy selected remote path"))
        self.copy_path_button.setToolTip(
            tr("Copy the exact remote path. This does not run a command.")
        )
        self.new_folder_button = WrappingPushButton(tr("New folder…"))
        self.new_folder_button.setAccessibleName(tr("Create a remote folder"))
        self.new_folder_button.setToolTip(
            tr("Create one folder in this SSH owner's current directory.")
        )
        self.secondary_actions_layout.addWidget(
            AdaptiveButtonRow((self.copy_path_button, self.new_folder_button))
        )
        self.edit_file_button = WrappingPushButton(tr("Edit text…"))
        self.rename_file_button = WrappingPushButton(tr("Rename/move…"))
        self.secondary_actions_layout.addWidget(
            AdaptiveButtonRow((self.edit_file_button, self.rename_file_button))
        )
        self._file_tools = SftpFileTools(self)
        self.edit_file_button.clicked.connect(self._file_tools.edit)
        self.rename_file_button.clicked.connect(self._file_tools.rename)
        self.status = ElidingLabel(tr("Loading directory…"))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        # Status and listing count share one stable line. Do not resize the file
        # list when a local tool reports feedback or an operation fails.
        layout.removeWidget(self.listing_summary)
        self.footer = QStackedWidget()
        self.footer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.footer.addWidget(self.listing_summary)
        self.footer.addWidget(self.status)
        self.footer.setCurrentWidget(self.status)
        self.status.text_changed.connect(
            lambda text: self.footer.setCurrentWidget(self.status if text else self.listing_summary)
        )
        layout.addWidget(self.footer)
        for widget in (
            self.owner_label,
            self.connection_badge,
            self.path_input,
            self.back_button,
            self.forward_button,
            self.up_button,
            self.refresh_button,
            self.files,
            self.filter_input,
            self.listing_summary,
            self.upload_button,
            self.upload_many_button,
            self.upload_folder_button,
            self.download_button,
            self.copy_path_button,
            self.new_folder_button,
            self.status,
        ):
            scale_widget_font(widget, 1.0)
        scale_widget_font(note, 0.95)
        self.path_input.returnPressed.connect(lambda: self.navigate(self.path_input.text()))
        self.refresh_button.clicked.connect(lambda: self.navigate(self._directory or "."))
        self.up_button.clicked.connect(
            lambda: self.navigate(posixpath.dirname(self._directory or "/") or "/")
        )
        self.back_button.clicked.connect(lambda: self._visit_history(-1))
        self.forward_button.clicked.connect(lambda: self._visit_history(1))
        self.files.cellDoubleClicked.connect(self._open_entry)
        self.files.itemSelectionChanged.connect(self._update_buttons)
        self.filter_input.textChanged.connect(self._apply_filter)
        self.copy_path_button.clicked.connect(self._copy_selected_path)
        self.new_folder_button.clicked.connect(self._new_folder)
        self.upload_button.clicked.connect(self._upload)
        self.upload_many_button.clicked.connect(self._upload_many)
        self.upload_folder_button.clicked.connect(self._folder_transfers.upload)
        self.download_button.clicked.connect(self._download)
        backend.sftp_result.connect(self._result)
        backend.sftp_progress.connect(self._progress)
        backend.finished.connect(self._ended)
        backend.destroyed.connect(self.dispose)
        self._file_icons = {}
        self._navigation_shortcuts = []
        for sequence, callback in (
            ("Alt+Left", lambda: self._visit_history(-1)),
            ("Alt+Right", lambda: self._visit_history(1)),
            ("Alt+Up", lambda: self.navigate(posixpath.dirname(self._directory or "/") or "/")),
            ("F5", lambda: self.navigate(self._directory or ".")),
            ("Ctrl+L", self._focus_path),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._navigation_shortcuts.append(shortcut)
        for sequence in ("Return", "Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self.files)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda: self._open_entry(self.files.currentRow(), 0))
            self._navigation_shortcuts.append(shortcut)
        self.set_interface_colors(DEFAULT_THEME)
        self._local_tools = SftpBrowserTools(self, details_layout)
        for key in ("actions", "display"):
            self.tools_dialog.page_layouts[key].addStretch()
        for button in (self.upload_button, self.download_button, self._local_tools.cancel_button):
            button.setAutoRaise(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(18, 18))
            button.setProperty("navigationRole", "remoteDirectory")
            button.setAccessibleName(button.text())
            if not button.toolTip():
                button.setToolTip(button.text())
        toolbar = AdaptiveButtonRow(
            (
                self.refresh_button,
                self.upload_button,
                self.download_button,
                self.secondary_actions_button,
                self._local_tools.cancel_button,
            )
        )
        toolbar.setObjectName("sftpCompactToolbar")
        toolbar.row_layout.setSpacing(2)
        layout.insertWidget(1, toolbar)
        self.secondary_actions_button.setToolTip(tr("More remote file actions"))
        self.set_interface_colors(DEFAULT_THEME)
        self._update_buttons()

    def _toggle_secondary_actions(self, expanded):
        if expanded:
            self._show_file_tools("actions")
        else:
            self.tools_dialog.reject()

    def _show_file_tools(self, key):
        if not self.available:
            with QSignalBlocker(self.secondary_actions_button):
                self.secondary_actions_button.setChecked(False)
            return
        with QSignalBlocker(self.secondary_actions_button):
            self.secondary_actions_button.setChecked(True)
        self.tools_dialog.move(self.mapToGlobal(self.rect().topRight()))
        self.tools_dialog.show_page(key)

    def _file_tools_closed(self, *_):
        with QSignalBlocker(self.secondary_actions_button):
            self.secondary_actions_button.setChecked(False)

    def hideEvent(self, event):
        if hasattr(self, "tools_dialog") and self.tools_dialog.isVisible():
            self.tools_dialog.reject()
        super().hideEvent(event)

    def set_interface_colors(self, colors: ThemeColors, *, monochrome: bool = False):
        tones = workbench_icon_colors(colors, monochrome=monochrome)
        for button, name in (
            (self.back_button, "arrow_left"),
            (self.forward_button, "arrow_right"),
            (self.up_button, "arrow_up"),
            (self.refresh_button, "refresh"),
            (self.upload_button, "upload"),
            (self.download_button, "download"),
            (self.secondary_actions_button, "settings"),
        ):
            button.setIcon(interface_icon(name, colors.text))
        if hasattr(self, "_local_tools"):
            self._local_tools.cancel_button.setIcon(interface_icon("stop", colors.danger))
        self._file_icons = {
            "directory": selection_interface_icon("folder", tones["folder"], colors.accent_text),
            "file": selection_interface_icon("file", colors.text, colors.accent_text),
        }
        if self._parent_row_count:
            self.files.item(0, 0).setIcon(self._file_icons["directory"])
        for row, entry in enumerate(self._items, self._parent_row_count):
            if entry.kind in self._file_icons:
                self.files.item(row, 0).setIcon(self._file_icons[entry.kind])

    @property
    def available(self):
        return (
            isValid(self)
            and not self._closed
            and not self._session_ended
            and isValid(self._backend)
            and self._backend.sftp_available
        )

    def _directory_ready(self, directory):
        return (
            directory is not None
            and self.available
            and self._directory == directory
            and self._listing is None
        )

    def _modal_context_changed(self):
        if isValid(self):
            self.status.setText(
                tr("The SSH folder or selection changed. Choose the file action again.")
            )

    def _update_buttons(self):
        live = self.available
        self.secondary_actions_button.setEnabled(live)
        entry = self._selected_entry()
        directory_ready = live and self._listing is None and self._directory is not None
        self.upload_button.setEnabled(directory_ready)
        self.upload_many_button.setEnabled(directory_ready)
        self.upload_folder_button.setEnabled(directory_ready)
        selected = self._selected_entries()
        self.download_button.setEnabled(
            directory_ready
            and bool(selected)
            and all(item.kind in ("file", "directory") for item in selected)
        )
        self.copy_path_button.setEnabled(directory_ready and entry is not None)
        regular_selected = directory_ready and entry is not None and entry.kind == "file"
        self.edit_file_button.setEnabled(regular_selected)
        self.rename_file_button.setEnabled(regular_selected)
        self.new_folder_button.setEnabled(directory_ready)
        self.filter_input.setEnabled(self._directory is not None and not self._closed)
        for widget in (self.path_input, self.up_button, self.refresh_button):
            widget.setEnabled(live)
        self.back_button.setEnabled(directory_ready and self._history_index > 0)
        self.forward_button.setEnabled(
            directory_ready and self._history_index + 1 < len(self._directory_history)
        )
        for shortcut in getattr(self, "_navigation_shortcuts", ()):
            shortcut.setEnabled(live)
        if hasattr(self, "_local_tools"):
            self._local_tools.update()
        if hasattr(self, "_places"):
            self._places.update_state()

    def _submit(self, request):
        if not self.available or not self._backend.request_sftp(request):
            self.status.setText(
                tr(
                    "Cannot start: SSH ended or queue is full ({p0} jobs maximum).",
                    p0=f"{MAX_SFTP_PENDING}",
                )
            )
            return False
        self._pending[request.request_id] = request
        if request.operation is not SftpOperation.LIST:
            self._publish_transfer(SftpTransferUpdate(request, tr("Queued"), True))
        if isValid(self):
            self._update_buttons()
        return True

    def navigate(self, path):
        return self._navigate(path)

    def _navigate(self, path, *, history_index=None):
        try:
            if self._directory is not None and not posixpath.isabs(path):
                path = posixpath.join(self._directory, path)
            request = SftpRequest(SftpOperation.LIST, path)
        except ValueError:
            self.status.setText(tr("Enter a valid remote directory."))
            return False
        previous_listing = self._listing
        if self._submit(request):
            self._listing = request.request_id
            self._listing_history_index = history_index
            if previous_listing is not None:
                self._backend.cancel_sftp(previous_listing)
            self.status.setText(tr("Loading directory…"))
            self._update_buttons()
            return True
        return False

    def _visit_history(self, direction):
        index = self._history_index + direction
        if self.available and self._listing is None and 0 <= index < len(self._directory_history):
            return self._navigate(self._directory_history[index], history_index=index)
        return False

    def _remember_directory(self, directory, history_index):
        """Commit history only after the current server listing succeeds."""
        if history_index is not None:
            self._directory_history[history_index] = directory
            self._history_index = history_index
        elif self._history_index < 0 or self._directory_history[self._history_index] != directory:
            del self._directory_history[self._history_index + 1 :]
            self._directory_history.append(directory)
            if len(self._directory_history) > MAX_DIRECTORY_HISTORY:
                del self._directory_history[0]
            self._history_index = len(self._directory_history) - 1

    def _focus_path(self):
        if self.available and self.isVisible():
            self.path_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.path_input.selectAll()
            self.body_scroll.ensureWidgetVisible(self.path_input)

    def _selected_entry(self):
        entries = self._selected_entries()
        return entries[0] if len(entries) == 1 else None

    def _selected_entries(self):
        return tuple(
            self._items[index.row() - self._parent_row_count]
            for index in sorted(
                self.files.selectionModel().selectedRows(), key=lambda index: index.row()
            )
            if self._parent_row_count <= index.row() < len(self._items) + self._parent_row_count
            and not self.files.isRowHidden(index.row())
        )

    def _apply_filter(self, _text=""):
        """Hide physical rows without changing their remote entry identities."""
        needle = self.filter_input.text().casefold()
        shown = 0
        for row, name in enumerate(self._filter_names):
            visible = needle in name and (
                not hasattr(self, "_local_tools")
                or self._local_tools.entry_visible(self._items[row])
            )
            table_row = row + self._parent_row_count
            if self.files.isRowHidden(table_row) != (not visible):
                self.files.setRowHidden(table_row, not visible)
            shown += int(visible)
        row = self.files.currentRow()
        if row >= 0 and self.files.isRowHidden(row):
            self.files.selectionModel().clearCurrentIndex()
        for index in tuple(self.files.selectionModel().selectedRows()):
            if self.files.isRowHidden(index.row()):
                self.files.selectionModel().select(
                    index,
                    QItemSelectionModel.SelectionFlag.Deselect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
        if self._directory is None:
            self.listing_summary.setText(tr("No directory loaded"))
        else:
            suffix = tr(" · Listing limited") if self._listing_truncated else ""
            if self._omitted_entries:
                suffix += tr(" · {p0} unsupported entries omitted", p0=f"{self._omitted_entries}")
            self.listing_summary.setText(
                tr(
                    "{p0} of {p1} loaded entries{p2}",
                    p0=f"{shown}",
                    p1=f"{len(self._items)}",
                    p2=f"{suffix}",
                )
            )
        self._update_buttons()

    def _copy_selected_path(self):
        entry = self._selected_entry()
        if (
            self.available
            and self._directory is not None
            and self._listing is None
            and entry is not None
        ):
            # A path is data, not a shell command. Display escaping must not
            # silently rename a Unicode/control-containing remote file.
            copied = copy_plain_text(posixpath.join(self._directory, entry.name))
            self.status.setText(
                tr("Remote path copied. No command was run.")
                if copied
                else tr(
                    "Clipboard unavailable or changed. Path copy could not be confirmed; try again."
                )
            )

    def _open_entry(self, row, _column):
        if row == 0 and self._parent_row_count and self._directory_ready(self._directory):
            self.navigate(posixpath.dirname(self._directory.rstrip("/")) or "/")
            return
        row -= self._parent_row_count
        if (
            self._directory is not None
            and 0 <= row < len(self._items)
            and not self.files.isRowHidden(row + self._parent_row_count)
            and self._items[row].kind == "directory"
        ):
            self.navigate(posixpath.join(self._directory, self._items[row].name))

    def _upload(self):
        directory = self._directory
        if not self._directory_ready(directory):
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("Upload file"))
        if path:
            if not self._directory_ready(directory):
                self._modal_context_changed()
                return
            try:
                local = Path(path).absolute()
                request = SftpRequest(
                    SftpOperation.UPLOAD, posixpath.join(directory, local.name), local
                )
            except (OSError, ValueError):
                self.status.setText(tr("The selected source path cannot be used for this upload."))
                return
            self._submit(request)

    def _submit_batch(self, requests, directory):
        if not self._directory_ready(directory):
            self._modal_context_changed()
            return 0
        while len(self._batches) >= MAX_SFTP_BATCH_CONTEXTS:
            completed = next(
                (key for key, value in self._batches.items() if not value.pending), None
            )
            if completed is None:
                self.status.setText(tr("Too many active file batches. Wait for a batch to finish."))
                return 0
            del self._batches[completed]
        batch = SftpBatchContext(directory)
        self._batches[batch.batch_id] = batch
        accepted = 0
        for request in requests:
            if not isValid(self) or self._closed:
                return accepted
            if self._submit(request):
                accepted += 1
                batch.pending.add(request.request_id)
                self._request_batches[request.request_id] = batch.batch_id
            else:
                self._publish_transfer(
                    SftpTransferUpdate(
                        request,
                        tr("Not queued · SSH ended or queue full"),
                        False,
                        result_status="error",
                    )
                )
        if not batch.pending:
            self._batches.pop(batch.batch_id, None)
        if isValid(self):
            kind = "items" if any(request.operation.is_tree for request in requests) else "files"
            kind_label = count_noun(kind, kind, len(requests))
            self.status.setText(
                tr(
                    "{p0} of {p1} {p2} queued; unqueued items were not sent.",
                    p0=f"{accepted}",
                    p1=f"{len(requests)}",
                    p2=kind_label,
                )
            )
        return accepted

    def _upload_many(self):
        directory = self._directory
        if not self._directory_ready(directory):
            return
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Upload files to this SSH folder"))
        if paths:
            if not isValid(self) or not self._directory_ready(directory):
                self._modal_context_changed()
                return
            try:
                requests = upload_batch(tuple(Path(path).absolute() for path in paths), directory)
            except (OSError, ValueError):
                self.status.setText(
                    tr(
                        "Select up to 64 regular files with distinct destination names; folders and links are unsupported."
                    )
                )
                return
            self._submit_batch(requests, directory)

    def _upload_drop(self, paths):
        directory = self._directory
        if not self._directory_ready(directory):
            return
        try:
            requests = upload_selection(paths, directory)
        except (OSError, ValueError):
            self.status.setText(
                tr("Drop regular local files/folders with safe, distinct names; not links.")
            )
            return
        if any(request.operation.is_tree for request in requests):
            if self._folder_transfers._confirm(requests, self._folder_transfers._context()):
                self._submit_batch(requests, directory)
            return
        message = QMessageBox(self)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWindowTitle(tr("Upload dropped files?"))
        names = "\n".join(file_label(request.local_path.name)[:120] for request in requests[:8])
        message.setText(
            tr(
                "SSH owner: {p0}\nFolder: {p1}\n\n{p2} files\n{p3}\n\nCopy these files? Local originals will not be moved or deleted.",
                p0=f"{self.owner_label.text()}",
                p1=f"{file_label(directory)}",
                p2=f"{len(requests)}",
                p3=f"{names}",
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        message.setDefaultButton(QMessageBox.StandardButton.Cancel)
        self._overwrite_dialog = message
        try:
            accepted = message.exec() == QMessageBox.StandardButton.Yes
        finally:
            self._overwrite_dialog = None
            if isValid(message):
                message.deleteLater()
        if isValid(self) and accepted:
            self._submit_batch(requests, directory)

    def _new_folder(self):
        directory = self._directory
        if not self.available or directory is None or self._listing is not None:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr("New remote folder"))
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        # Static label: remote directory names cannot become rich-text markup.
        dialog.setLabelText(tr("Folder name in the current SSH directory (not a path):"))
        dialog.setOkButtonText(tr("Create folder"))
        dialog.setCancelButtonText("Cancel")
        self._folder_dialog = dialog
        try:
            accepted = dialog.exec() == QInputDialog.DialogCode.Accepted
            name = dialog.textValue() if isValid(dialog) else ""
        finally:
            self._folder_dialog = None
            if isValid(dialog):
                dialog.deleteLater()
        if not accepted or not isValid(self):
            return
        if not self._directory_ready(directory):
            self.status.setText(tr("The SSH folder changed or ended. Choose New folder again."))
            return
        if not name.strip() or name in (".", "..") or "/" in name or "\0" in name:
            self.status.setText(tr("Enter one folder name, not a path."))
            return
        try:
            request = SftpRequest(SftpOperation.MKDIR, posixpath.join(directory, name))
        except ValueError:
            self.status.setText(tr("The folder path is too long or unsupported."))
            return
        self._submit(request)

    def _download(self):
        entries = self._selected_entries()
        if entries and any(item.kind == "directory" for item in entries):
            if all(item.kind in ("file", "directory") for item in entries):
                self._folder_transfers.download(entries)
            return
        if len(entries) > 1:
            self._download_many(entries)
            return
        entry = self._selected_entry()
        directory = self._directory
        if entry is None or entry.kind != "file" or not self._directory_ready(directory):
            return
        remote_path = posixpath.join(directory, entry.name)
        # A remote name is not a local path, especially on Windows.
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Download file"), "", options=QFileDialog.Option.DontConfirmOverwrite
        )
        if path:
            if not self._directory_ready(directory):
                self._modal_context_changed()
                return
            selected = self._selected_entry()
            if selected is None or (selected.name, selected.kind) != (entry.name, entry.kind):
                self._modal_context_changed()
                return
            try:
                request = SftpRequest(SftpOperation.DOWNLOAD, remote_path, Path(path).absolute())
            except (OSError, ValueError):
                self.status.setText(
                    tr("The selected destination path cannot be used for this download.")
                )
                return
            self._submit(request)

    def _download_many(self, entries):
        directory = self._directory
        if not self._directory_ready(directory) or any(entry.kind != "file" for entry in entries):
            return
        target = QFileDialog.getExistingDirectory(
            self, tr("Download selected files into directory")
        )
        if not target:
            return
        if (
            not isValid(self)
            or not self._directory_ready(directory)
            or self._selected_entries() != entries
        ):
            self._modal_context_changed()
            return
        try:
            requests = download_batch(entries, directory, Path(target).absolute())
        except (OSError, ValueError):
            self.status.setText(
                tr(
                    "Choose up to 64 regular files with safe, distinct local names, or download individually with another name."
                )
            )
            return
        self._submit_batch(requests, directory)

    def cancel_transfer(self, request_id):
        transfer = self._transfers.get(request_id)
        if (
            self.available
            and transfer is not None
            and transfer.pending
            and self._backend.cancel_sftp(request_id)
        ):
            self._publish_transfer(replace(transfer, status=tr("Cancelling…")))

    def _publish_transfer(self, transfer):
        if transfer.request.request_id not in self._transfers:
            while len(self._transfers) >= MAX_JOB_HISTORY:
                candidate = next(
                    (key for key, item in self._transfers.items() if not item.pending), None
                )
                if candidate is None:
                    break
                del self._transfers[candidate]
        self._transfers[transfer.request.request_id] = transfer
        self.transfer_changed.emit(transfer)
        if isValid(self) and hasattr(self, "_local_tools"):
            self._local_tools.update()

    def _render_entries(self, entries, selected=()):
        self.files.clearSelection()
        self.files.setCurrentCell(-1, -1)
        self._items = list(entries)
        self._parent_row_count = int(self._directory not in (None, "/"))
        self._filter_names = [entry.name.casefold() for entry in self._items]
        self.files.setRowCount(len(self._items) + self._parent_row_count)
        if self._parent_row_count:
            parent = QTableWidgetItem(self._file_icons.get("directory"), "..")
            parent.setToolTip(tr("Parent directory (Alt+Up)"))
            self.files.setItem(0, 0, parent)
            self.files.setItem(0, 1, QTableWidgetItem(""))
            self.files.setRowHidden(0, False)
        for row, entry in enumerate(self._items, self._parent_row_count):
            for column, value in enumerate(
                (
                    file_label(entry.name),
                    str(entry.size) if entry.kind == "file" and entry.size is not None else "—",
                )
            ):
                item = QTableWidgetItem(value)
                item.setTextAlignment(FILE_COLUMN_ALIGNMENTS[column])
                self.files.setItem(row, column, item)
            item = self.files.item(row, 0)
            item.setToolTip(
                plain_text_tooltip(
                    f"{file_label(entry.name)} ({tr(entry.kind, context='file-kind')})"
                )
            )
            if entry.kind in self._file_icons:
                item.setIcon(self._file_icons[entry.kind])
        self._apply_filter()
        for row, entry in enumerate(self._items, self._parent_row_count):
            if (entry.name, entry.kind) in selected and not self.files.isRowHidden(row):
                self.files.selectionModel().select(
                    self.files.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
                if self.files.currentRow() < 0:
                    self.files.setCurrentCell(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        self._update_buttons()

    def _progress(self, event):
        transfer = self._transfers.get(event.request_id)
        if transfer is not None and event.request_id in self._pending:
            self._publish_transfer(
                replace(
                    transfer,
                    status=(
                        tr("Scanning folders…")
                        if event.phase == "planning"
                        else tr(
                            "Preparing folders · {p0}/{p1}",
                            p0=f"{event.directories_completed}",
                            p1=f"{event.directories_total}",
                        )
                        if event.phase == "folders"
                        else tr(
                            "Transferring · {p0}/{p1} files",
                            p0=f"{event.files_completed}",
                            p1=f"{event.files_total}",
                        )
                        if transfer.request.operation.is_tree
                        else tr("Transferring")
                    ),
                    transferred=event.transferred,
                    total=event.total,
                )
            )

    def _result(self, result):
        request = self._pending.pop(result.request_id, None)
        if request is None or self._closed:
            return
        result_text = sftp_result_text(request, result)
        batch_id = self._request_batches.pop(result.request_id, None)
        batch = self._batches.get(batch_id)
        if batch is not None and result.status != "overwrite-required":
            batch.pending.discard(result.request_id)
        if request.operation is SftpOperation.LIST:
            if result.request_id != self._listing:
                return  # A late result cannot replace a newer navigation.
            self._listing = None
            history_index, self._listing_history_index = self._listing_history_index, None
            if result.status == "ok":
                self._remember_directory(result.directory, history_index)
                previous_entries = {(entry.name, entry.kind) for entry in self._selected_entries()}
                same_directory = self._directory == result.directory
                self._directory = result.directory
                self._places.directory_changed(result.directory)
                self.path_input.setText(result.directory)
                self._loaded_items = list(result.entries)
                self._listing_generation += 1
                self._listing_truncated = result.truncated
                self._omitted_entries = result.omitted_entries
                self._render_entries(
                    self._local_tools.ordered(self._loaded_items),
                    previous_entries if same_directory else (),
                )
                self.status.setText(
                    tr("Directory listing limited; enter a subdirectory path to navigate further.")
                    if result.truncated
                    else ""
                )
            else:
                self.status.setText(result_text)
                if self._directory is not None:
                    self.path_input.setText(self._directory)
        else:
            if request.operation.is_tree:
                self.status.setText(result_text)
            self._publish_transfer(
                replace(
                    self._transfers[result.request_id],
                    status=result_text,
                    pending=False,
                    result_status=result.status,
                )
            )
            if result.status == "overwrite-required":
                if len(self._conflicts) < MAX_SFTP_PENDING:
                    self._conflicts.append((request, batch))
                    if not self._conflict_active:
                        self._drain_conflicts()
                elif batch is not None:
                    batch.pending.discard(request.request_id)
                    self.status.setText(
                        tr("Too many file conflicts; choose the file action again.")
                    )
            elif (
                result.status == "ok"
                or (
                    request.operation is SftpOperation.UPLOAD_TREE
                    and result.tree_summary is not None
                    and (
                        result.tree_summary.files_completed
                        or result.tree_summary.directories_created
                    )
                )
            ) and (
                request.operation
                in (
                    SftpOperation.UPLOAD,
                    SftpOperation.UPLOAD_TREE,
                    SftpOperation.EDIT_UPLOAD,
                    SftpOperation.RENAME,
                )
                or (
                    request.operation is SftpOperation.MKDIR
                    and posixpath.dirname(request.remote_path) == self._directory
                )
            ):
                if self._listing is None:
                    self.navigate(self._directory)
        if isValid(self):
            self._update_buttons()

    def _retry_transfer(self, request, batch, *, overwrite=False, renamed=None, tree_policy=None):
        if not self.available:
            return
        arguments = {"overwrite": overwrite, "request_id": uuid4()}
        if request.operation.is_tree:
            arguments["overwrite"] = False
            arguments["tree_policy"] = (
                tree_policy
                if tree_policy is not None
                else SftpTreePolicy.REPLACE
                if overwrite
                else SftpTreePolicy.ASK
            )
        if renamed is not None:
            if request.operation in (SftpOperation.DOWNLOAD, SftpOperation.DOWNLOAD_TREE):
                arguments["local_path"] = request.local_path.with_name(renamed)
            else:
                arguments["remote_path"] = posixpath.join(
                    posixpath.dirname(request.remote_path), renamed
                )
        fresh = replace(request, **arguments)
        if self._submit(fresh) and batch is not None:
            batch.pending.add(fresh.request_id)
            self._request_batches[fresh.request_id] = batch.batch_id

    def _drain_conflicts(self):
        if not isValid(self) or self._conflict_active:
            return
        self._conflict_active = True
        try:
            while self._conflicts and self.available:
                request, batch = self._conflicts.popleft()
                try:
                    if batch is not None and batch.policy == "skip":
                        if request.operation.is_tree:
                            self._retry_transfer(request, batch, tree_policy=SftpTreePolicy.SKIP)
                            continue
                        self._publish_transfer(
                            replace(
                                self._transfers[request.request_id],
                                status=tr("Skipped · batch policy"),
                                pending=False,
                                result_status="cancelled",
                            )
                        )
                        continue
                    if batch is not None and batch.policy == "replace":
                        self._retry_transfer(request, batch, overwrite=True)
                        continue
                    self._review_conflict(request, batch)
                finally:
                    if batch is not None:
                        batch.pending.discard(request.request_id)
                if not isValid(self):
                    return
        finally:
            self._conflict_active = False

    def _review_conflict(self, request, batch):
        if request.operation.is_tree:
            self._folder_transfers.review_conflict(request, batch)
            return
        directory = self._directory
        destination = (
            str(request.local_path)
            if request.operation is SftpOperation.DOWNLOAD
            else request.remote_path
        )
        message = QMessageBox(self)
        message.setWindowTitle(tr("Replace existing file?"))
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setText(
            tr(
                "SSH owner: {p0}\nDestination already exists:\n{p1}\n\nReplace its contents? The existing file is kept until the transfer completes.",
                p0=f"{self.owner_label.text()}",
                p1=f"{file_label(destination)}",
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        message.setDefaultButton(QMessageBox.StandardButton.Cancel)
        checkbox = None
        rename_button = None
        if batch is not None:
            checkbox = ThemedCheckBox(
                tr("Use this replace/skip decision for remaining conflicts in this batch")
            )
            message.setCheckBox(checkbox)
            rename_button = message.addButton(
                tr("Choose another name…"), QMessageBox.ButtonRole.ActionRole
            )
        self._overwrite_dialog = message
        try:
            accepted = message.exec() == QMessageBox.StandardButton.Yes
            rename = (
                isValid(message)
                and rename_button is not None
                and message.clickedButton() is rename_button
            )
            applies = checkbox is not None and isValid(checkbox) and checkbox.isChecked()
        finally:
            self._overwrite_dialog = None
            if isValid(message):
                message.deleteLater()
        if (
            not isValid(self)
            or not self.available
            or self._directory != directory
            or self._listing is not None
        ):
            if isValid(self):
                self._modal_context_changed()
            return
        if rename:
            dialog = QInputDialog(self)
            dialog.setLabelText(tr("New destination filename (not a path):"))
            self._folder_dialog = dialog
            try:
                chosen = dialog.exec() == QInputDialog.DialogCode.Accepted
                name = dialog.textValue() if isValid(dialog) else ""
            finally:
                self._folder_dialog = None
                if isValid(dialog):
                    dialog.deleteLater()
            if chosen and isValid(self) and self._directory_ready(directory):
                from termana.models.sftp_batch import local_download_name

                try:
                    local_download_name(name)
                    self._retry_transfer(request, batch, renamed=name)
                except (ValueError, OSError):
                    self.status.setText(tr("Choose one supported filename, not a path."))
            return
        if batch is not None and applies:
            batch.policy = "replace" if accepted else "skip"
        if accepted:
            self._retry_transfer(request, batch, overwrite=True)
        elif batch is not None:
            self._publish_transfer(
                replace(
                    self._transfers[request.request_id],
                    status=tr("Skipped · user decision"),
                    pending=False,
                    result_status="cancelled",
                )
            )

    def _ended(self, _code):
        self.tools_dialog.reject()
        self._local_tools.close_menu()
        self._file_tools.ended()
        self._session_ended = True
        self._conflicts.clear()
        if self._overwrite_dialog is not None:
            self._overwrite_dialog.reject()
        if self._folder_dialog is not None:
            self._folder_dialog.reject()
        self.status.setText(tr("SSH session ended. Reconnect and open Files again."))
        self._finish_pending_transfers()
        self._update_buttons()

    def _finish_pending_transfers(self):
        for transfer in tuple(self._transfers.values()):
            if transfer.pending:
                self._publish_transfer(
                    replace(
                        transfer,
                        status=tr(
                            "SSH ended; check the destination for a partial or completed transfer."
                        ),
                        pending=False,
                        result_status="error",
                    )
                )

    def dispose(self):
        if self._closed:
            return
        self._closed = True
        self._places.dispose()
        self._file_tools.dispose()
        self._conflicts.clear()
        self._request_batches.clear()
        self._batches.clear()
        if not isValid(self):
            # Qt already disconnected this dead receiver. Only owned request
            # cancellation remains safe; never emit or touch child widgets.
            if isValid(self._backend):
                for request_id in tuple(self._pending):
                    self._backend.cancel_sftp(request_id)
            self._pending.clear()
            return
        self._local_tools.close_menu()
        self.tools_dialog.reject()
        if self._overwrite_dialog is not None:
            self._overwrite_dialog.reject()
        if self._folder_dialog is not None:
            self._folder_dialog.reject()
        if isValid(self._backend):
            for request_id in tuple(self._pending):
                self._backend.cancel_sftp(request_id)
        self._pending.clear()
        self._finish_pending_transfers()
        if isValid(self._backend):
            self._backend.sftp_result.disconnect(self._result)
            self._backend.sftp_progress.disconnect(self._progress)
            self._backend.finished.disconnect(self._ended)
            self._backend.destroyed.disconnect(self.dispose)
