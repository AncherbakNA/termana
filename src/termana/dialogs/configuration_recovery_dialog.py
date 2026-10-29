"""User-driven recovery before any live stores, sessions or keychain are loaded."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from termana.storage.configuration_backup import (
    BACKUPS,
    ConfigurationBackupError,
    prepare_restore,
    prepare_settings_restore,
)
from termana.storage.configuration_recovery import restore_for_recovery
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.inline_error_area import InlineErrorArea
from termana.widgets.wrapping_label import WrappingLabel


class ConfigurationRecoveryDialog(QDialog):
    def __init__(self, directory: Path, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.prepared = None
        self.setObjectName("configurationRecoveryDialog")
        self.setWindowTitle("Recover configuration")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(14)
        scroll = QScrollArea(self)
        scroll.setObjectName("configurationRecoveryBody")
        scroll.setWidgetResizable(True)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        description = WrappingLabel(
            "A scheduled configuration restore could not finish safely. No sessions or saved "
            "passwords have been opened. Choose a trusted Termana backup or an automatic "
            "settings snapshot, or exit to review the retained files."
        )
        description.setTextFormat(Qt.TextFormat.PlainText)
        body_layout.addWidget(description)
        self.selection = WrappingLabel("No backup selected.")
        self.selection.setTextFormat(Qt.TextFormat.PlainText)
        body_layout.addWidget(self.selection)
        self.errors = InlineErrorArea(self)
        body_layout.addWidget(self.errors)
        body_layout.addStretch()
        choose = QPushButton("Choose backup…", self)
        choose.setObjectName("chooseRecoveryBackupButton")
        choose.clicked.connect(self.choose_backup)
        folder = QPushButton("Open backup folder", self)
        folder.setObjectName("openRecoveryFolderButton")
        folder.clicked.connect(self.open_folder)
        self.restore_button = QPushButton("Restore and retry", self)
        self.restore_button.setObjectName("restoreAndRetryButton")
        self.restore_button.setProperty("actionRole", "primary")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self.restore)
        exit_button = QPushButton("Exit", self)
        exit_button.setObjectName("exitRecoveryButton")
        for button in (choose, folder, self.restore_button, exit_button):
            button.setAutoDefault(False)
            if button is not self.restore_button:
                button.setProperty("actionRole", "secondary")
        exit_button.setDefault(True)
        exit_button.clicked.connect(self.reject)
        layout.addWidget(
            AdaptiveButtonRow((choose, folder, self.restore_button, exit_button), self)
        )
        self.resize(720, 300)

    def open_folder(self):
        folder = self.directory / BACKUPS
        if not folder.is_dir():
            folder = self.directory
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def choose_backup(self):
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a trusted Termana backup",
            str(self.directory / BACKUPS),
            "Termana backup or settings snapshot (*.zip *.json)",
        )
        if not selected:
            return
        self.prepared = None
        self.restore_button.setEnabled(False)
        try:
            path = Path(selected)
            self.prepared = (
                prepare_restore(path)
                if path.suffix.lower() == ".zip"
                else prepare_settings_restore(path)
            )
        except (OSError, ConfigurationBackupError):
            self.selection.setText("No backup selected.")
            self.errors.show_message(
                "This backup is invalid, unsupported or could not be read safely. Current files are unchanged."
            )
            return
        self.errors.hide()
        self.selection.setText("Replace: " + ", ".join(self.prepared.files))
        self.restore_button.setEnabled(True)

    def _confirm(self):
        box = QMessageBox(self)
        box.setWindowTitle("Restore this configuration?")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText("Replace: " + ", ".join(self.prepared.files))
        box.setInformativeText(
            "The current affected files and previous restore state will be retained in "
            "configuration-backups before replacement. Other configuration files, passwords, "
            "keys and host trust are unchanged. Restore only your own trusted backup."
        )
        restore = box.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        accepted = box.clickedButton() is restore
        box.deleteLater()
        return accepted

    def restore(self):
        if self.prepared is None or not self._confirm():
            return
        try:
            restore_for_recovery(self.directory, self.prepared)
        except (OSError, ConfigurationBackupError):
            self.errors.show_message(
                "Recovery could not finish safely. Current or changed files and recovery copies have been kept. Open the backup folder for review, or exit."
            )
            return
        self.accept()


def show_configuration_recovery(directory: Path) -> bool:
    dialog = ConfigurationRecoveryDialog(directory)
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
