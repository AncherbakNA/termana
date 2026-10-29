"""Explicit, next-launch configuration recovery without touching live sessions."""

from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from termana.paths import application_config_directory
from termana.storage.configuration_backup import (
    BACKUPS,
    PENDING,
    ConfigurationBackupError,
    cancel_pending_restore,
    create_backup,
    prepare_restore,
    prepare_settings_restore,
    stage_restore,
)


class ConfigurationBackupUi(QObject):
    def __init__(self, parent: QWidget, *, directory: Path | None = None):
        super().__init__(parent)
        self._window = parent
        self._directory = directory

    def _path(self):
        return self._directory if self._directory is not None else application_config_directory()

    def _message(self, title, text, *, confirm=False):
        box = QMessageBox(self._window)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(text)
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
            if confirm
            else QMessageBox.StandardButton.Ok
        )
        box.setDefaultButton(
            QMessageBox.StandardButton.Cancel if confirm else QMessageBox.StandardButton.Ok
        )
        return box.exec() == QMessageBox.StandardButton.Ok

    def back_up(self):
        if not self._message(
            "Back up configuration",
            "The backup includes settings, saved sessions and Commands, but not saved SSH "
            "passwords, private keys or host trust. It is not encrypted; connection details "
            "and any sensitive text saved in Commands remain readable. Keep it private.",
            confirm=True,
        ):
            return
        destination, _ = QFileDialog.getSaveFileName(
            self._window,
            "Save configuration backup",
            "Termana-config.termana-backup.zip",
            "Termana backup (*.zip)",
        )
        if not destination:
            return
        try:
            names = create_backup(self._path(), Path(destination))
        except (OSError, ConfigurationBackupError):
            self._message(
                "Backup not saved",
                "Choose a new writable file. Existing files are never overwritten; configuration must be valid and within the size limit.",
            )
            return
        self._message("Backup saved", "Included: " + ", ".join(names))

    def restore(self):
        directory = self._path()
        if (directory / PENDING).exists():
            if self._message(
                "Restore already scheduled",
                "Cancel the scheduled restore? Its archive will be retained in configuration-backups. Current settings and sessions will not change.",
                confirm=True,
            ):
                try:
                    cancel_pending_restore(directory)
                except (OSError, ConfigurationBackupError):
                    self._message(
                        "Restore not cancelled",
                        "The request could not be moved safely. Restart Termana to recover any interrupted restore.",
                    )
            return
        archive, _ = QFileDialog.getOpenFileName(
            self._window,
            "Restore configuration",
            str(directory / BACKUPS),
            "Termana backup or settings snapshot (*.zip *.json)",
        )
        if not archive:
            return
        try:
            path = Path(archive)
            prepared = (
                prepare_restore(path)
                if path.suffix.lower() == ".zip"
                else prepare_settings_restore(path)
            )
            names = prepared.files
            if not self._message(
                "Restore on next launch?",
                "Replace: " + ", ".join(names) + ".\n\n"
                "Nothing changes now. Quit Termana normally, then reopen it. The current "
                "files will first be kept in configuration-backups. Passwords, keys and host "
                "trust are unchanged. Restore only your own trusted backup.",
                confirm=True,
            ):
                return
            stage_restore(directory, prepared)
        except (OSError, ConfigurationBackupError):
            self._message(
                "Restore not scheduled",
                "The backup is invalid, unsupported, too large or could not be saved safely. Current configuration is unchanged.",
            )
            return
        self._message(
            "Restore scheduled",
            "Quit and reopen Termana when ready. To cancel before quitting, choose Restore configuration again.",
        )
