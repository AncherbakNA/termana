"""Visible, bounded paste preview; opening this dialog never sends terminal input."""

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from termana.i18n import tr, validation_text
from termana.terminal.paste import MAX_PASTE_CHARACTER_COUNT, PasteTooLargeError, prepare_paste
from termana.widgets.numbered_plain_text_edit import NumberedPlainTextEdit


class PastePreviewDialog(QDialog):
    def __init__(
        self, text: str, *, unprotected: bool, encoding="utf-8", one_line=False, parent=None
    ):
        super().__init__(parent)
        self._encoding = encoding
        self._one_line = one_line
        self.setObjectName("pasteConfirmationDialog")
        self.setWindowTitle(tr("Confirm terminal paste"))
        layout = QVBoxLayout(self)
        label = QLabel(
            tr("Line breaks may run commands immediately. Review the text before pasting.")
            if unprotected
            else tr("Review the text before pasting."),
            self,
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.preview = NumberedPlainTextEdit(self)
        self.preview.setObjectName("pasteContentPreview")
        self.preview.setPlainText(text)
        self.preview.setAccessibleName(tr("Text to paste"))
        self.preview.setAccessibleDescription(
            tr("Edit the text before pasting. Line numbers are not sent.")
        )
        layout.addWidget(self.preview)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.buttons = QDialogButtonBox(self)
        self.paste_button = paste = self.buttons.addButton(
            tr("Paste"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        paste.setObjectName("confirmPasteButton")
        paste.setProperty("actionRole", "primary")
        paste.setAutoDefault(False)
        cancel = self.buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setObjectName("cancelPasteButton")
        cancel.setProperty("actionRole", "secondary")
        cancel.setDefault(True)
        for button in (paste, cancel):
            button.style().unpolish(button)
            button.style().polish(button)
            button.updateGeometry()
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.preview.textChanged.connect(self._check_size)
        layout.addWidget(self.buttons)
        self.resize(580, 360)

    @property
    def paste_text(self) -> str:
        return self.preview.plain_text()

    def _check_size(self):
        # QTextDocument counts UTF-16 code units; the terminal limit counts
        # Unicode characters, so emoji must not consume two characters here.
        too_large = len(self.paste_text) > MAX_PASTE_CHARACTER_COUNT
        self.paste_button.setEnabled(not too_large)
        self.notice.setVisible(too_large)
        if too_large:
            self.notice.setText(tr("Shorten the text before pasting (maximum 131,072 characters)."))

    def accept(self):
        try:
            original = self.paste_text
            reviewed = prepare_paste(original, bracketed=False, one_line=self._one_line).plain_text
            reviewed = reviewed.encode(self._encoding, errors="replace").decode(
                self._encoding, errors="replace"
            )
        except PasteTooLargeError as error:
            self.notice.setText(validation_text(str(error)))
            self.notice.show()
            return
        if reviewed != original:
            # Never approve invisible control/encoding/one-line transformations.
            # Display the actual outgoing text and require another explicit Paste.
            self.preview.setPlainText(reviewed)
            self.notice.setText(
                tr("Text was adjusted for this terminal. Review it and press Paste again.")
            )
            self.notice.show()
            return
        super().accept()
