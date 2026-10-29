"""Bounded literal replacement of one local draft; no transport capabilities."""

from dataclasses import dataclass

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import QLineEdit, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from termana.i18n import tr
from termana.models.sftp import MAX_SFTP_EDIT_BYTES
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.document_search import MAX_DOCUMENT_MATCHES
from termana.widgets.wrapping_label import WrappingLabel

MAX_LOCAL_REPLACEMENTS = MAX_DOCUMENT_MATCHES


@dataclass(frozen=True, slots=True)
class LocalReplacementPlan:
    ranges: tuple[tuple[int, int], ...]
    replacement: str


def plan_local_replacement(
    document,
    query,
    replacement,
    text_format,
    *,
    case_sensitive=False,
    whole_words=False,
    replace_all=False,
    start=0,
):
    """Find with Qt's existing semantics and validate final wire bytes before edits.

    Ranges are UTF-16 positions, not Python string offsets. Keep at most 1000
    matches and reject an over-cap all operation, never silently replace a prefix.
    Wire pieces are counted before joining so repeated large replacements cannot
    allocate an unbounded candidate. An empty range set is an inert no-op.
    """
    if not query:
        raise ValueError("Type a literal Find query first.")
    query.encode("utf-8")
    replacement.encode("utf-8")
    flags = QTextDocument.FindFlag(0)
    if case_sensitive:
        flags |= QTextDocument.FindFlag.FindCaseSensitively
    if whole_words:
        flags |= QTextDocument.FindFlag.FindWholeWords
    finder = QTextCursor(document)
    finder.setPosition(0 if replace_all else start)
    matches = []
    while True:
        finder = document.find(query, finder, flags)
        if finder.isNull():
            if not replace_all and start:
                finder = document.find(query, QTextCursor(document), flags)
            if finder.isNull():
                break
        if len(matches) == MAX_LOCAL_REPLACEMENTS:
            raise ValueError("More than 1000 matches. Refine Find before Replace all.")
        matches.append((finder.selectionStart(), finder.selectionEnd()))
        if not replace_all:
            break
    if not matches:
        return LocalReplacementPlan((), replacement)

    encoded = document.toPlainText().encode("utf-16-le")
    pieces, changes = [], []
    previous = 0
    for first, last in matches:
        pieces.append(encoded[previous * 2 : first * 2].decode("utf-16-le"))
        pieces.append(replacement)
        if encoded[first * 2 : last * 2].decode("utf-16-le") != replacement:
            changes.append((first, last))
        previous = last
    pieces.append(encoded[previous * 2 :].decode("utf-16-le"))
    bom_bytes = 3 if text_format.bom else 0
    wire_bytes = bom_bytes
    for piece in pieces:
        wire_bytes += len(text_format.encode(piece)) - bom_bytes
        if wire_bytes > MAX_SFTP_EDIT_BYTES:
            raise ValueError(tr("Replacement would exceed 2 MiB. Local text was not changed."))
    # Validate the exact complete candidate as well as the bounded piece budget.
    text_format.encode("".join(pieces))
    return LocalReplacementPlan(tuple(changes), replacement)


class SftpTextReplace(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText(tr("Replace with literal text (empty removes matches)"))
        self.replacement.setAccessibleName(tr("Replacement text in local edited copy"))
        self.replacement.setMaxLength(MAX_SFTP_EDIT_BYTES)
        self.replacement.installEventFilter(self)
        layout.addWidget(self.replacement)
        self.replace_next_button = QPushButton(tr("Replace next"))
        self.replace_all_button = QPushButton(tr("Replace all"))
        for button, label, callback in (
            (
                self.replace_next_button,
                tr("Replace next match in local edited copy"),
                self.replace_next,
            ),
            (
                self.replace_all_button,
                tr("Replace all matches in local edited copy"),
                self.replace_all,
            ),
        ):
            button.setAccessibleName(label)
            button.setProperty("actionRole", "secondary")
            button.setAutoDefault(False)
            button.clicked.connect(callback)
            button.setEnabled(False)
        layout.addWidget(AdaptiveButtonRow((self.replace_next_button, self.replace_all_button)))
        self.feedback = WrappingLabel(
            tr("Literal local text only · Up to 1000 matches · Save to server remains explicit.")
        )
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.setAccessibleName(tr("Local replacement result"))
        layout.addWidget(self.feedback)
        owner.search.query.textChanged.connect(self.update_state)

    def _mutable(self):
        return (
            isValid(self)
            and isValid(self.owner)
            and not self.owner._closed
            and not self.owner._pending
            and not self.owner._close_review_active
            and not self.owner.editor.isReadOnly()
        )

    def update_state(self, *_):
        enabled = self._mutable() and bool(self.owner.search.query.text())
        self.replace_next_button.setEnabled(enabled)
        self.replace_all_button.setEnabled(enabled)

    def focus_replacement(self):
        if not isValid(self.owner) or self.owner._closed:
            return
        self.owner.search.show()
        self.show()
        field = self.replacement if self.owner.search.query.text() else self.owner.search.query
        field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        field.selectAll()

    def replace_next(self):
        self._replace(replace_all=False)

    def replace_all(self):
        self._replace(replace_all=True)

    def _replace(self, *, replace_all):
        if not self._mutable():
            return
        editor = self.owner.editor
        document = editor.document()
        revision = document.revision()
        try:
            plan = plan_local_replacement(
                document,
                self.owner.search.query.text(),
                self.replacement.text(),
                self.owner._text_format,
                case_sensitive=self.owner.search.case_sensitive.isChecked(),
                whole_words=self.owner.search.whole_words.isChecked(),
                replace_all=replace_all,
                start=editor.textCursor().selectionStart(),
            )
        except (ValueError, UnicodeError):
            self.feedback.setText(
                tr(
                    "Not replaced. Use a non-empty literal query, at most 1000 matches and valid UTF-8 text up to 2 MiB without NUL or unsupported separators."
                )
            )
            return
        if not self._mutable() or document.revision() != revision:
            return
        if not plan.ranges:
            self.feedback.setText(tr("No local text changes. No SSH request was sent."))
            return
        transaction = QTextCursor(document)
        transaction.beginEditBlock()
        try:
            for first, last in reversed(plan.ranges):
                cursor = QTextCursor(document)
                cursor.setPosition(first)
                cursor.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
                cursor.insertText(plan.replacement)
        finally:
            transaction.endEditBlock()
        cursor = QTextCursor(document)
        cursor.setPosition(plan.ranges[0][0] + len(plan.replacement.encode("utf-16-le")) // 2)
        editor.setTextCursor(cursor)
        self.owner._measure_text()
        self.feedback.setText(
            tr(
                "Replaced {p0} match(es) locally. No SSH request was sent.",
                p0=f"{len(plan.ranges)}",
            )
        )

    def eventFilter(self, watched, event):
        if watched is self.replacement and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                return True  # Never trigger a dialog default or implicit remote save.
            if event.key() == Qt.Key.Key_Escape and not event.modifiers():
                self.hide()
                self.owner.editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
                return True
        return super().eventFilter(watched, event)
