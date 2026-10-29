"""Dialog for creating or renaming one saved-connection folder."""

from collections.abc import Collection
from dataclasses import replace
from typing import Final
from unicodedata import normalize

from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible, QAccessibleEvent, QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr, validation_text
from termana.models.connection import (
    MAX_CONNECTION_FOLDER_NAME_CHARACTERS,
    ConnectionFolder,
)
from termana.ui_typography import scale_widget_font

MINIMUM_FOLDER_DIALOG_WIDTH: Final = 380


def _comparison_key(name: str) -> str:
    """Normalize one sibling label for user-facing uniqueness checks."""
    return normalize("NFC", name).casefold()


class ConnectionFolderDialog(QDialog):
    """Validate a new folder label or rename an existing immutable folder."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        folder: ConnectionFolder | None = None,
        reserved_names: Collection[str] = (),
    ) -> None:
        """Build a create/rename form scoped to one set of sibling names."""
        super().__init__(parent)
        self.setObjectName("connectionFolderDialog")
        self.setWindowTitle(tr("Rename folder") if folder is not None else tr("New folder"))
        self.setModal(True)
        self.setMinimumWidth(MINIMUM_FOLDER_DIALOG_WIDTH)

        self._original_folder = folder
        self._connection_folder: ConnectionFolder | None = None
        self._reserved_name_keys = {
            _comparison_key(name.strip())
            for name in reserved_names
            if isinstance(name, str) and name.strip()
        }
        if folder is not None:
            self._reserved_name_keys.discard(_comparison_key(folder.name))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel(tr("Rename folder") if folder is not None else tr("New folder"))
        title.setObjectName("dialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        scale_widget_font(title, 1.7, weight=QFont.Weight.Bold)

        description = QLabel(
            tr("Choose a new name for this saved-connections folder.")
            if folder is not None
            else tr("Create a folder for organizing saved connections.")
        )
        description.setObjectName("mutedText")
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self.name_input = QLineEdit()
        self.name_input.setObjectName("connectionFolderNameInput")
        self.name_input.setAccessibleName(tr("Connection folder name"))
        # Qt limits UTF-16 units; the model validates Unicode code points.
        self.name_input.setMaxLength(2 * (MAX_CONNECTION_FOLDER_NAME_CHARACTERS + 1))
        self.name_input.setPlaceholderText(tr("Production"))

        name_label = QLabel(tr("Name"))
        name_label.setBuddy(self.name_input)
        form.addRow(name_label, self.name_input)

        self.validation_error = QLabel()
        self.validation_error.setObjectName("connectionFolderValidationError")
        self.validation_error.setAccessibleName(tr("Connection folder validation error"))
        self.validation_error.setTextFormat(Qt.TextFormat.PlainText)
        self.validation_error.setWordWrap(True)
        self.validation_error.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setObjectName("connectionFolderDialogButtons")
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setObjectName("saveConnectionFolderButton")
        self.save_button.setEnabled(False)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setObjectName("cancelConnectionFolderButton")

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.name_input.textChanged.connect(self._handle_name_changed)

        layout.addWidget(title)
        layout.addWidget(description)
        layout.addSpacing(4)
        layout.addLayout(form)
        layout.addWidget(self.validation_error)
        layout.addWidget(buttons)

        if folder is not None:
            self.name_input.setText(folder.name)
            self.name_input.selectAll()

    @property
    def connection_folder(self) -> ConnectionFolder:
        """Return the validated folder only after the dialog was accepted."""
        if self._connection_folder is None:
            raise RuntimeError("Connection folder dialog has not been accepted")
        return self._connection_folder

    def accept(self) -> None:
        """Validate sibling uniqueness and construct an immutable folder record."""
        name = self.name_input.text().strip()
        self._handle_name_changed()
        if not name:
            self.name_input.setFocus()
            return
        if _comparison_key(name) in self._reserved_name_keys:
            self._show_validation_error(tr("A folder with this name already exists here."))
            return

        try:
            folder = (
                ConnectionFolder(name=name)
                if self._original_folder is None
                else replace(self._original_folder, name=name)
            )
        except (TypeError, ValueError) as error:
            self._show_validation_error(validation_text(str(error)))
            return

        self._connection_folder = folder
        super().accept()

    def _handle_name_changed(self) -> None:
        """Clear obsolete feedback and enable Save for nonblank input."""
        self.validation_error.clear()
        self.validation_error.setAccessibleName(tr("Connection folder validation error"))
        self.validation_error.hide()
        self.save_button.setEnabled(bool(self.name_input.text().strip()))

    def _show_validation_error(self, message: str) -> None:
        """Expose validation failures visually and to assistive technology."""
        self.validation_error.setText(message)
        self.validation_error.setAccessibleName(
            tr("Connection folder validation error: {p0}", p0=f"{message}")
        )
        self.validation_error.show()
        self.name_input.setFocus()
        self.name_input.selectAll()
        QAccessible.updateAccessibility(
            QAccessibleEvent(self.validation_error, QAccessible.Event.Alert)
        )
