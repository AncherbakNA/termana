"""A bounded, inert UTF-8 editor; downloads never launch external programs."""

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from termana.i18n import tr
from termana.initial_window_fit import InitialWindowFit
from termana.models.remote_text import decode_remote_text
from termana.models.sftp import MAX_SFTP_EDIT_BYTES
from termana.terminal.private_text_export import export_private_text_copy
from termana.ui_typography import scale_widget_font
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.document_search import DocumentSearch
from termana.widgets.form_controls import ThemedSpinBox
from termana.widgets.sftp_text_replace import SftpTextReplace
from termana.widgets.themed_checkbox import ThemedCheckBox
from termana.widgets.wrapping_label import WrappingLabel

EDITOR_MINIMUM_VISIBLE_LINES = 6
EDITOR_TOOLS_PREFERRED_HEIGHT = 240


def edited_bytes(text):
    data = text.encode("utf-8")
    if len(data) > MAX_SFTP_EDIT_BYTES or "\0" in text:
        raise ValueError("Only UTF-8 text up to 2 MiB is supported")
    return data


class SftpTextEditor(QDialog):
    save_requested = Signal()
    closed = Signal()

    def __init__(self, parent, *, title, data, checkpoint=None):
        text, self._text_format = decode_remote_text(data)
        super().__init__(parent)
        if len(data) > MAX_SFTP_EDIT_BYTES or b"\0" in data:
            raise ValueError("Unsupported text file")
        self.setWindowTitle(tr("Edit remote text"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(760, 520)
        self._initial_fit = InitialWindowFit(self, allowed=self._prepare_initial_fit)
        layout = QVBoxLayout(self)
        self.label = WrappingLabel(title)
        self.label.setObjectName("settingsSectionTitle")
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.editor = QPlainTextEdit()
        scale_widget_font(self.editor, 1.0, monospace=True)
        self.editor.installEventFilter(self)
        # Newlines are normalized for Qt only; wire bytes retain their format.
        self.editor.setPlainText(text)
        if self.editor.toPlainText() != text:
            raise ValueError("This file's line separators cannot be preserved by the text editor")
        layout.addWidget(self.editor, 1)
        self.editor.setAccessibleName(tr("Remote text local edited copy"))
        self.tools_scroll = QScrollArea()
        self.tools_scroll.setObjectName("remoteEditorTools")
        self.tools_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tools_scroll.setWidgetResizable(True)
        self.tools_scroll.setSizeAdjustPolicy(QScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.tools_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tools_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        tools_body = QWidget()
        tools_layout = QVBoxLayout(tools_body)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        self.tools_scroll.setWidget(tools_body)
        tools_body.installEventFilter(self)
        layout.addWidget(self.tools_scroll)
        self.search = DocumentSearch(
            self.editor, self, label=tr("Find in local edited copy"), editable=True
        )
        self.search.hide()
        tools_layout.addWidget(self.search)
        self.replace_tools = SftpTextReplace(self)
        self.replace_tools.hide()
        tools_layout.addWidget(self.replace_tools)
        self.find_button = QPushButton(tr("Find…"))
        self.find_button.clicked.connect(self.search.focus_query)
        self.replace_button = QPushButton(tr("Replace…"))
        self.replace_button.setAccessibleName(tr("Show replacement tools for local edited copy"))
        self.replace_button.clicked.connect(self.replace_tools.focus_replacement)
        self.undo_button = QPushButton(tr("Undo"))
        self.undo_button.setAccessibleName(tr("Undo local text change"))
        self.undo_button.clicked.connect(self._undo)
        self.redo_button = QPushButton(tr("Redo"))
        self.redo_button.setAccessibleName(tr("Redo local text change"))
        self.redo_button.clicked.connect(self._redo)
        self.line_input = ThemedSpinBox()
        self.line_input.setRange(1, self.editor.document().blockCount())
        self.line_input.setPrefix(tr("Line "))
        self.line_input.setAccessibleName(tr("Go to local text line"))
        self.go_button = QPushButton(tr("Go"))
        self.go_button.clicked.connect(self._go_to_line)
        self.wrap = ThemedCheckBox(tr("Wrap lines"))
        self.wrap.setChecked(True)
        self.wrap.toggled.connect(
            lambda checked: self.editor.setLineWrapMode(
                QPlainTextEdit.LineWrapMode.WidgetWidth
                if checked
                else QPlainTextEdit.LineWrapMode.NoWrap
            )
        )
        tools_layout.addWidget(
            AdaptiveButtonRow(
                (
                    self.find_button,
                    self.replace_button,
                    self.undo_button,
                    self.redo_button,
                    self.line_input,
                    self.go_button,
                    self.wrap,
                )
            )
        )
        self.details = WrappingLabel("")
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setAccessibleName(tr("Local text cursor and file format"))
        tools_layout.addWidget(self.details)
        self.recovery_status = WrappingLabel(
            tr("Private recovery checkpoints are local. Recent typing may not yet be checkpointed.")
            if checkpoint is not None
            else tr("Crash recovery is unavailable for this copy.")
        )
        self.recovery_status.setTextFormat(Qt.TextFormat.PlainText)
        tools_layout.addWidget(self.recovery_status)
        self.note = WrappingLabel(
            tr("Local copy · Save to server is explicit. Remote version checked before replace.")
        )
        self.note.setWordWrap(True)
        tools_layout.addWidget(self.note)
        self.save_button = QPushButton(tr("Save to server"))
        self.export_button = QPushButton(tr("Save local copy…"))
        self.close_button = QPushButton(tr("Close"))
        self.footer = AdaptiveButtonRow((self.save_button, self.export_button, self.close_button))
        layout.addWidget(self.footer)
        self.footer.installEventFilter(self)
        self.save_button.clicked.connect(self.save_requested)
        self.export_button.clicked.connect(self._export)
        self.close_button.clicked.connect(self.close)
        self._live = True
        self._pending = False
        self._closing = False
        self._close_review_active = False
        self._closed = False
        self._valid_text = True
        self._byte_count = len(data)
        self._checkpoint_callback = checkpoint
        self._checkpoint_timer = QTimer(self)
        self._checkpoint_timer.setSingleShot(True)
        self._checkpoint_timer.timeout.connect(self._checkpoint)
        self._measure_timer = QTimer(self)
        self._measure_timer.setSingleShot(True)
        self._measure_timer.timeout.connect(self._measure_text)
        self._save_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Save), self)
        self._save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._save_shortcut.activated.connect(self._request_save)
        for button in (
            self.save_button,
            self.export_button,
            self.close_button,
            self.find_button,
            self.replace_button,
            self.undo_button,
            self.redo_button,
            self.go_button,
        ):
            button.setAutoDefault(False)
            button.setProperty(
                "actionRole", "primary" if button is self.save_button else "secondary"
            )
        self.editor.textChanged.connect(self._text_changed)
        self.editor.cursorPositionChanged.connect(self._update_details)
        self.editor.document().modificationChanged.connect(self._update_state)
        self.editor.document().undoAvailable.connect(self._update_state)
        self.editor.document().redoAvailable.connect(self._update_state)
        self.editor.document().setModified(False)
        self._update_state()
        self._update_details()
        self._update_control_geometry()

    def _update_control_geometry(self):
        if not hasattr(self, "tools_scroll"):
            return
        self.editor.setMinimumHeight(
            self.editor.fontMetrics().lineSpacing() * EDITOR_MINIMUM_VISIBLE_LINES
            + 2 * self.editor.frameWidth()
        )
        cap = min(EDITOR_TOOLS_PREFERRED_HEIGHT, max(1, self.height() // 2))
        if hasattr(self, "footer") and (screen := self.screen()) is not None:
            margins = self.layout().contentsMargins()
            content_width = max(1, self.width() - margins.left() - margins.right())
            label_height = self.label.heightForWidth(content_width)
            borders = max(0, self.frameGeometry().height() - self.height())
            reserved = (
                max(0, label_height)
                + self.editor.minimumHeight()
                + self.footer.minimumSizeHint().height()
                + margins.top()
                + margins.bottom()
                + max(0, self.layout().spacing()) * (self.layout().count() - 1)
            )
            cap = min(cap, max(1, screen.availableGeometry().height() - borders - reserved))
        self.tools_scroll.setMaximumHeight(cap)
        # QScrollArea caches the collapsed sizeHint. Reserve the current tools'
        # natural height up to the cap when Find/Replace changes the body layout.
        self.tools_scroll.setMinimumHeight(
            min(cap, self.tools_scroll.widget().minimumSizeHint().height())
        )

    def eventFilter(self, watched, event):
        if hasattr(self, "tools_scroll") and (
            (watched is self.editor and event.type() == QEvent.Type.FontChange)
            or (
                (watched is self.tools_scroll.widget() or watched is getattr(self, "footer", None))
                and event.type() == QEvent.Type.LayoutRequest
            )
        ):
            QTimer.singleShot(0, self, self._update_control_geometry)
        return super().eventFilter(watched, event)

    def _prepare_initial_fit(self):
        self._update_control_geometry()
        self.layout().activate()
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_control_geometry()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._update_control_geometry()

    def _go_to_line(self):
        if self._closed:
            return
        block = self.editor.document().findBlockByNumber(self.line_input.value() - 1)
        if block.isValid():
            self.editor.setTextCursor(QTextCursor(block))
            self.editor.ensureCursorVisible()
            self.editor.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _request_save(self):
        if self._measure_timer.isActive():
            self._measure_text()
        if not self._closed and self.save_button.isEnabled():
            self.save_requested.emit()

    def _undo(self):
        if not self._closed and not self._pending and not self.editor.isReadOnly():
            self.editor.undo()
            self._measure_text()

    def _redo(self):
        if not self._closed and not self._pending and not self.editor.isReadOnly():
            self.editor.redo()
            self._measure_text()

    def _text_changed(self):
        self.line_input.setMaximum(max(1, self.editor.document().blockCount()))
        self._valid_text = False  # Do not authorize Save against stale byte feedback.
        self._byte_count = None
        self._update_state()
        self._measure_timer.start(80)
        if self._checkpoint_callback is not None:
            self._checkpoint_timer.start(750)
        self._update_details()

    def _checkpoint(self):
        self._checkpoint_timer.stop()
        if self._closed or self._checkpoint_callback is None:
            return
        try:
            data = self.encoded_text()
            confirmed = self._checkpoint_callback(data)
        except Exception:
            confirmed = False
        self.recovery_status.setText(
            tr("Private recovery checkpoint saved locally. No SSH request was sent.")
            if confirmed
            else tr("Recovery checkpoint unavailable. Use Save local copy to keep this edit.")
        )

    def _measure_text(self):
        self._measure_timer.stop()
        if self._closed:
            return
        try:
            self._byte_count = len(self.encoded_text())
            self._valid_text = True
        except (ValueError, UnicodeError):
            self._byte_count = None
            self._valid_text = False
        self._update_state()
        self._update_details()

    def _update_state(self, *_):
        dirty = self.editor.document().isModified()
        self.setWindowTitle(tr("Edit remote text") + (tr(" · Unsaved") if dirty else ""))
        self.save_button.setEnabled(
            self._live and not self._pending and not self._closed and dirty and self._valid_text
        )
        mutable = not self._closed and not self._pending and not self.editor.isReadOnly()
        self.undo_button.setEnabled(mutable and self.editor.document().isUndoAvailable())
        self.redo_button.setEnabled(mutable and self.editor.document().isRedoAvailable())
        self.replace_button.setEnabled(not self._closed)
        self.replace_tools.update_state()

    def _update_details(self):
        cursor = self.editor.textCursor()
        newline = {"\n": "LF", "\r\n": "CRLF", "\r": "CR"}[self._text_format.newline]
        size = (
            tr("{p0} / {p1} bytes", p0=f"{self._byte_count:,}", p1=f"{MAX_SFTP_EDIT_BYTES:,}")
            if self._byte_count is not None
            else tr("Checking text size…")
            if self._measure_timer.isActive()
            else tr("Invalid text or over 2 MiB · Save disabled")
        )
        self.details.setText(
            tr(
                "Line {p0}, column {p1} (UTF-16) · UTF-8{p2} · {p3} · {p4}",
                p0=f"{cursor.blockNumber() + 1}",
                p1=f"{cursor.positionInBlock() + 1}",
                p2=f"{(' BOM' if self._text_format.bom else '')}",
                p3=f"{newline}",
                p4=f"{size}",
            )
        )

    def encoded_text(self):
        return self._text_format.encode(self.editor.toPlainText())

    def set_live(self, live):
        self._live = live
        self._update_state()
        if not live:
            self.note.setText(
                tr("SSH owner ended · Remote save disabled. You can still save a local copy.")
            )

    def set_pending(self, pending):
        self._pending = pending
        self.editor.setReadOnly(pending)
        self._update_state()
        self.close_button.setEnabled(not pending)

    def _export(self):
        if self._closed:
            return
        try:
            data = self.encoded_text()
        except (ValueError, UnicodeError):
            self.note.setText(tr("Choose valid text up to 2 MiB before exporting."))
            return
        revision = self.editor.document().revision()
        path, _ = QFileDialog.getSaveFileName(self, tr("Save local edited copy"))
        if not path or not isValid(self) or self._closed:
            return
        if revision != self.editor.document().revision():
            self.note.setText(
                tr("The edit changed while the picker was open. Choose Save local copy again.")
            )
            return
        # New path only. A file picker overwrite confirmation isn't authority
        # to overwrite a file which changed while the picker was open.
        try:
            export_private_text_copy(Path(path), data)
            self.note.setText(tr("Local copy saved; no SSH request was sent."))
        except (OSError, ValueError):
            self.note.setText(
                tr(
                    "Local export could not be confirmed. Inspect the chosen folder, then choose a new filename in a supported private local folder. Existing files are never replaced."
                )
            )

    def reject(self):
        # QDialog's default Escape path bypasses closeEvent. Route it through
        # the exact dirty/pending guard, but allow Qt's final internal reject.
        if self._closing:
            super().reject()
        else:
            self.close()

    def closeEvent(self, event):
        if self._pending or self._close_review_active:
            event.ignore()
            return
        if self.editor.document().isModified():
            self._close_review_active = True
            try:
                decision = QMessageBox.question(
                    self,
                    tr("Close local edit?"),
                    tr("Unsaved edits will be discarded. Close this copy?"),
                    QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
            finally:
                self._close_review_active = False
            if not isValid(self):
                return
            if self._pending:
                event.ignore()
                return
            if decision != QMessageBox.StandardButton.Discard:
                event.ignore()
                return
        self._closed = True
        self._measure_timer.stop()
        self._checkpoint_timer.stop()
        self.search._timer.stop()
        self._closing = True
        try:
            self.closed.emit()
            if isValid(self):
                super().closeEvent(event)
        finally:
            self._closing = False
