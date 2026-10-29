"""Local snapshot tools; never execute commands or create another SSH owner."""

import posixpath
from datetime import UTC, datetime

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
)
from shiboken6 import isValid

from termana.i18n import tr
from termana.ui_typography import scale_widget_font
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.clipboard_text import copy_plain_text
from termana.widgets.themed_checkbox import ThemedCheckBox
from termana.widgets.wrapping_button import WrappingPushButton
from termana.widgets.wrapping_label import WrappingLabel

MAX_COPIED_PATHS = 64


def properties_text(owner, directory, entry):
    """Listing snapshot, not fresh stat and not an assertion of access rights."""
    try:
        modified = (
            datetime.fromtimestamp(entry.modified, UTC).isoformat()
            if entry.modified is not None
            else tr("Unknown")
        )
    except (ValueError, OverflowError, OSError):
        modified = tr("Outside displayable UTC range")
    permissions = (
        f"{entry.permissions & 0o777:03o}" if entry.permissions is not None else tr("Unknown")
    )
    size = (
        str(entry.size)
        if entry.kind == "file" and entry.size is not None
        else tr("Unknown / not a regular file")
    )
    return tr(
        "SSH owner: {p0}\nPath: {p1}\nType: {p2}\nBytes: {p3}\nOrdinary permissions (octal): {p4}\nModified (UTC): {p5}\n\nLoaded listing snapshot only. Refresh to request newer metadata.\nPermissions do not describe ACLs or guarantee your effective access. No symlink is followed.",
        p0=f"{owner}",
        p1=f"{posixpath.join(directory, entry.name)}",
        p2=tr(entry.kind, context="file-kind"),
        p3=f"{size}",
        p4=f"{permissions}",
        p5=f"{modified}",
    )


class SftpBrowserTools(QObject):
    def __init__(self, browser, layout):
        super().__init__(browser)
        self.browser = browser
        self._menu = None
        self._properties = None
        self.hidden = ThemedCheckBox(tr("Show hidden"))
        self.hidden.setChecked(True)  # Preserve the existing all-entries default.
        self.hidden.setAccessibleName(tr("Show loaded hidden remote entries"))
        self.kind = QComboBox()
        for label, value in (
            (tr("All types"), ""),
            (tr("Files"), "file"),
            (tr("Folders"), "directory"),
            (tr("Links"), "link"),
            (tr("Other"), "other"),
        ):
            self.kind.addItem(label, value)
        self.kind.setAccessibleName(tr("Filter loaded remote entry types"))
        self.order = QComboBox()
        for label, value in (
            (tr("Listing order"), ""),
            (tr("Name A-Z"), "name"),
            (tr("Name Z-A"), "name-desc"),
            (tr("Size: smallest"), "size"),
            (tr("Size: largest"), "size-desc"),
        ):
            self.order.addItem(label, value)
        self.order.setAccessibleName(tr("Sort loaded remote entries"))
        for selector in (self.kind, self.order):
            # Popup choices must not force the whole scroll body wider than its
            # viewport. The adaptive row still measures the preferred caption.
            selector.setMinimumWidth(0)
            policy = selector.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            selector.setSizePolicy(policy)
        self.selection = WrappingLabel(tr("No entries selected"))
        self.selection.setObjectName("sftpSelectionSummary")
        self.selection.setTextFormat(Qt.TextFormat.PlainText)
        self.selection.setAccessibleName(tr("Selected remote entries"))
        self.properties_button = WrappingPushButton(tr("Properties…"))
        self.properties_button.setAutoDefault(False)
        self.cancel_button = QToolButton()
        self.cancel_button.setText(tr("Cancel this session's file jobs"))
        self.cancel_button.setToolTip(
            tr(
                "Requests cooperative cancellation only for this SSH owner's current file jobs. Committed operations are not undone."
            )
        )
        self.options_button = QToolButton()
        self.options_button.setCheckable(True)
        self.options_button.setAutoRaise(True)
        self.options_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.options_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.options_button.setProperty("navigationRole", "remoteDirectory")
        self.options_button.setAccessibleName(tr("Remote file display options"))
        self.options_button.setToolTip(tr("Show or hide file type, hidden files and sort options."))
        scale_widget_font(self.options_button, 1.0)
        self.options_row = AdaptiveButtonRow((self.hidden, self.kind, self.order))
        self.options_row.hide()
        layout.addWidget(self.options_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.options_row)
        layout.addWidget(self.selection)
        browser.secondary_actions_layout.addWidget(AdaptiveButtonRow((self.properties_button,)))
        self.options_button.toggled.connect(self._toggle_options)
        self.hidden.toggled.connect(self._update_options_label)
        self.kind.currentIndexChanged.connect(self._update_options_label)
        self.order.currentIndexChanged.connect(self._update_options_label)
        self._update_options_label()
        self._toggle_options(False)
        self.hidden.toggled.connect(browser._apply_filter)
        self.kind.currentIndexChanged.connect(browser._apply_filter)
        self.order.currentIndexChanged.connect(self.sort)
        self.properties_button.clicked.connect(self.properties)
        self.cancel_button.clicked.connect(self.cancel_all)
        browser.files.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        browser.files.customContextMenuRequested.connect(self.context_menu)
        self.shortcuts = []
        for sequence, callback, owner in (
            (QKeySequence(QKeySequence.StandardKey.Find), self.focus_filter, browser),
            (QKeySequence("Ctrl+Shift+C"), self.copy_paths, browser.files),
            (QKeySequence("Alt+Return"), self.properties, browser.files),
        ):
            shortcut = QShortcut(sequence, owner)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        browser._backend.finished.connect(self.close_menu)

    def _toggle_options(self, expanded):
        focused = QApplication.focusWidget()
        restore_focus = (
            not expanded and focused is not None and self.options_row.isAncestorOf(focused)
        )
        self.options_row.setVisible(expanded)
        self.options_button.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        if restore_focus:
            self.options_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _update_options_label(self, *_):
        modified = sum(
            (
                not self.hidden.isChecked(),
                bool(self.kind.currentData()),
                bool(self.order.currentData()),
            )
        )
        label = tr("Display options")
        self.options_button.setText(label + (f" · {modified}" if modified else ""))
        self.options_button.setAccessibleDescription(
            label + (" · " + tr("Modified") if modified else "")
        )

    def entry_visible(self, entry):
        return (self.hidden.isChecked() or not entry.name.startswith(".")) and (
            not self.kind.currentData() or entry.kind == self.kind.currentData()
        )

    def ordered(self, entries):
        mode = self.order.currentData()
        if not mode:
            return list(entries)
        reverse = mode.endswith("-desc")

        def key(entry):
            if mode.startswith("size"):
                # Unknown sizes stay after known regular files in both orders.
                known = entry.kind == "file" and entry.size is not None
                return (
                    not known,
                    (-entry.size if reverse else entry.size) if known else 0,
                    entry.name.casefold(),
                    entry.name,
                )
            return (entry.name.casefold(), entry.name)

        dirs = [entry for entry in entries if entry.kind == "directory"]
        rest = [entry for entry in entries if entry.kind != "directory"]
        name_reverse = reverse and mode.startswith("name")
        return sorted(dirs, key=key, reverse=name_reverse) + sorted(
            rest, key=key, reverse=name_reverse
        )

    def sort(self, *_):
        view = self.browser
        if not isValid(view) or view._closed:
            return
        selected = {(entry.name, entry.kind) for entry in view._selected_entries()}
        view._render_entries(self.ordered(view._loaded_items), selected)

    def update(self):
        view = self.browser
        selected = view._selected_entries()
        sizes = [
            entry.size for entry in selected if entry.kind == "file" and entry.size is not None
        ]
        self.selection.setText(
            tr("{p0} selected · {p1} known file bytes", p0=f"{len(selected)}", p1=f"{sum(sizes):,}")
            + (tr(" · some sizes unknown/non-file") if len(sizes) != len(selected) else "")
            if selected
            else tr("No entries selected")
        )
        self.selection.setVisible(bool(selected))
        ready = view._directory_ready(view._directory)
        self.properties_button.setEnabled(ready and len(selected) == 1)
        self.cancel_button.setEnabled(
            view.available and any(job.pending for job in view._transfers.values())
        )
        self.cancel_button.setVisible(self.cancel_button.isEnabled())
        for shortcut in self.shortcuts:
            shortcut.setEnabled(view.available)

    def focus_filter(self):
        view = self.browser
        if view.available and view.isVisible():
            view._show_file_tools("display")
            view.filter_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
            view.filter_input.selectAll()
            view.tools_dialog.pages["display"].ensureWidgetVisible(view.filter_input)

    def copy_paths(self):
        view = self.browser
        entries = view._selected_entries()
        if not view._directory_ready(view._directory) or not entries:
            return
        if len(entries) > MAX_COPIED_PATHS:
            view.status.setText(tr("Select up to 64 paths to copy at once."))
            return
        copied = copy_plain_text(
            "\n".join(posixpath.join(view._directory, item.name) for item in entries)
        )
        view.status.setText(
            tr("{p0} exact remote paths copied as text. No command was run.", p0=f"{len(entries)}")
            if copied
            else tr(
                "Clipboard unavailable or changed. Path copy could not be confirmed; try again."
            )
        )

    def properties(self):
        view = self.browser
        entry = view._selected_entry()
        if not view._directory_ready(view._directory) or entry is None:
            return
        if self._properties is not None and isValid(self._properties):
            self._properties.close()
        dialog = QDialog(view)
        self._properties = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(tr("Remote entry properties · listing snapshot"))
        dialog.resize(540, 360)
        layout = QVBoxLayout(dialog)
        display = QPlainTextEdit()
        display.setReadOnly(True)
        display.setAccessibleName(tr("Remote entry properties snapshot"))
        display.setPlainText(properties_text(view.owner_label.text(), view._directory, entry))
        layout.addWidget(display)
        close = QPushButton(tr("Close"))
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        dialog.show()  # Inert snapshot; no nested consent or backend capability.
        return dialog

    def cancel_all(self):
        view = self.browser
        if not view.available:
            return
        for identity, job in tuple(view._transfers.items()):
            if job.pending:
                view.cancel_transfer(identity)
        self.update()

    def close_menu(self, *_):
        if self._menu is not None and isValid(self._menu):
            self._menu.close()

    def context_menu(self, position):
        view = self.browser
        if not view._directory_ready(view._directory):
            return
        index = view.files.indexAt(position)
        if not index.isValid() or view.files.isRowHidden(index.row()):
            return
        if not view.files.selectionModel().isRowSelected(index.row(), index.parent()):
            view.files.selectRow(index.row())
        context = (view._directory, view._listing_generation, view._selected_entries())
        selected = context[2]
        if not selected:
            return
        menu = QMenu(view.files)
        self._menu = menu
        actions = {}

        def add(label, callback, enabled=True):
            action = menu.addAction(label)
            action.setEnabled(enabled)
            actions[action] = callback

        single = selected[0] if len(selected) == 1 else None
        add(
            tr("Open folder"),
            lambda: view._open_entry(view.files.currentRow(), 0),
            single is not None and single.kind == "directory",
        )
        add(
            tr("Download…"),
            view._download,
            all(item.kind in ("file", "directory") for item in selected),
        )
        add(tr("Edit text…"), view._file_tools.edit, single is not None and single.kind == "file")
        add(
            tr("Rename/move…"),
            view._file_tools.rename,
            single is not None and single.kind == "file",
        )
        menu.addSeparator()
        add(
            tr("Copy paths") if len(selected) != 1 else tr("Copy path"),
            self.copy_paths,
            len(selected) <= MAX_COPIED_PATHS,
        )
        add(tr("Properties…"), self.properties, single is not None)
        try:
            chosen = self._exec_menu(menu, view.files.viewport().mapToGlobal(position))
            if (
                isValid(view)
                and view._directory_ready(context[0])
                and context == (view._directory, view._listing_generation, view._selected_entries())
            ):
                callback = actions.get(chosen)
                if callback:
                    callback()
        finally:
            self._menu = None
            if isValid(menu):
                menu.deleteLater()

    @staticmethod
    def _exec_menu(menu, position):
        return menu.exec(position)
