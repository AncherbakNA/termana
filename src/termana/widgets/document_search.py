"""Bounded local literal search, with native UTF-16 cursors; never regex/remote IO."""

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.themed_checkbox import ThemedCheckBox

MAX_DOCUMENT_MATCHES = 1000
DOCUMENT_SEARCH_DEBOUNCE_MS = 50


class DocumentSearch(QWidget):
    """Search only the supplied snapshot; never terminal output or external files."""

    def __init__(
        self,
        display: QPlainTextEdit,
        owner: QWidget,
        *,
        label=None,
        editable=False,
    ) -> None:
        super().__init__(owner)
        self._display = display
        self._matches: list[QTextCursor] = []
        self._index = -1
        self._editable = editable
        self._truncated = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.query = QLineEdit()
        caption = tr("Find in SSH details") if label is None else label
        self.query.setPlaceholderText(caption)
        self.query.setAccessibleName(caption)
        self.query.setMaxLength(256)
        self.query.setClearButtonEnabled(True)
        self.query.installEventFilter(self)
        layout.addWidget(self.query)
        self.case_sensitive = ThemedCheckBox(tr("Match case"))
        self.whole_words = ThemedCheckBox(tr("Whole words"))
        self.counter = QLabel(tr("Type to find"))
        self.counter.setTextFormat(Qt.TextFormat.PlainText)
        self.counter.setWordWrap(True)
        self.previous_button = QPushButton("↑")
        self.next_button = QPushButton("↓")
        for button, label, step in (
            (self.previous_button, tr("Previous match"), -1),
            (self.next_button, tr("Next match"), 1),
        ):
            button.setAccessibleName(label)
            button.setProperty("actionRole", "secondary")
            button.setToolTip(label + tr(" · Shift+Enter") if step < 0 else label + tr(" · Enter"))
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, step=step: self.navigate(step))
        layout.addWidget(
            AdaptiveButtonRow(
                (self.case_sensitive, self.whole_words, self.previous_button, self.next_button)
            )
        )
        layout.addWidget(self.counter)
        self.query.textChanged.connect(self._schedule)
        self.case_sensitive.toggled.connect(self._schedule)
        self.whole_words.toggled.connect(self._schedule)
        display.textChanged.connect(self._schedule)
        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), owner)
        self.find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.find_shortcut.activated.connect(self.focus_query)
        self._update_counter()

    def focus_query(self) -> None:
        self.show()
        self.query.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.query.selectAll()

    def _schedule(self) -> None:
        # A QTextCursor tracks edits, but an old result set no longer describes
        # this query/document. Disable navigation immediately, not after debounce.
        self._matches.clear()
        self._index = -1
        self._truncated = False
        if self._editable:
            self._display.setExtraSelections([])
        self._update_counter()
        self._timer.start(DOCUMENT_SEARCH_DEBOUNCE_MS)

    def _refresh(self) -> None:
        self._timer.stop()
        self._matches.clear()
        query = self.query.text()
        flags = QTextDocument.FindFlag(0)
        if self.case_sensitive.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self.whole_words.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        self._truncated = False
        cursor = QTextCursor(self._display.document())
        if query:
            while True:
                cursor = self._display.document().find(query, cursor, flags)
                if cursor.isNull():
                    break
                if len(self._matches) == MAX_DOCUMENT_MATCHES:
                    self._truncated = True
                    break
                self._matches.append(cursor)
        self._index = 0 if self._matches else -1
        self._show_match(move=not self._editable)

    def navigate(self, step: int) -> None:
        if self._timer.isActive():
            self._refresh()
            if self._matches:
                self._index = -1 if step > 0 else 0
        if self._matches:
            self._index = (self._index + step) % len(self._matches)
            self._show_match()

    def _show_match(self, *, move=True) -> None:
        if self._editable:
            selections = []
            if self._index >= 0:
                selection = QTextEdit.ExtraSelection()
                selection.cursor = self._matches[self._index]
                selection.format.setBackground(self._display.palette().highlight())
                selection.format.setForeground(self._display.palette().highlightedText())
                selections.append(selection)
            self._display.setExtraSelections(selections)
            if not move:
                self._update_counter()
                return
        if self._index >= 0:
            self._display.setTextCursor(self._matches[self._index])
            self._display.ensureCursorVisible()
        else:
            cursor = self._display.textCursor()
            cursor.clearSelection()
            self._display.setTextCursor(cursor)
        self._update_counter()

    def _update_counter(self) -> None:
        text = (
            tr("{p0} of {p1}", p0=f"{self._index + 1}", p1=f"{len(self._matches)}")
            if self._matches
            else tr("No matches")
            if self.query.text()
            else tr("Type to find")
        )
        if self._truncated:
            text += tr(" · first {p0} matches only", p0=f"{MAX_DOCUMENT_MATCHES}")
        self.counter.setText(text)
        self.counter.setAccessibleName(text)
        self.previous_button.setEnabled(bool(self._matches))
        self.next_button.setEnabled(bool(self._matches))

    def eventFilter(self, watched, event: QEvent) -> bool:
        if watched is self.query and event.type() == QEvent.Type.KeyPress:
            modifiers = event.modifiers()
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
                    self.navigate(-1 if modifiers else 1)
                # Modified Enter must not activate the dialog's default action.
                return True
            if event.key() == Qt.Key.Key_Escape and not modifiers and self.query.text():
                self.query.clear()
                return True
            if event.key() == Qt.Key.Key_Escape and not modifiers and self._editable:
                self.hide()
                self._display.setExtraSelections([])
                self._display.setFocus(Qt.FocusReason.ShortcutFocusReason)
                return True
        return super().eventFilter(watched, event)
