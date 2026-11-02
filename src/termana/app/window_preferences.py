"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from termana import APP_NAME
from termana.app.build_info import support_details, version_label
from termana.app.qt_lifecycle import schedule_delete_later
from termana.app.window_contracts import CONFIRMATION_MESSAGE_TIMEOUT_MS
from termana.models.settings import ApplicationSettings
from termana.storage.settings_store import SettingsStoreError
from termana.ui.dialogs.settings_dialog import SettingsDialog
from termana.ui.i18n import tr


def _open_settings_dialog(self: MainWindow, _checked: bool = False) -> None:
    """Open one modal editor and keep it open when persistence fails."""
    if self._close_waiting_for_terminals:
        return
    dialog = SettingsDialog(self, settings=self._settings_controller.settings)
    dialog.save_requested.connect(
        lambda candidate: self._commit_settings_from_dialog(dialog, candidate)
    )
    dialog.theme_preview_requested.connect(self._settings_controller.preview_theme)
    try:
        dialog.exec()
    finally:
        self._settings_controller.clear_theme_preview()
        schedule_delete_later(dialog)


def _commit_settings_from_dialog(
    self: MainWindow, dialog: SettingsDialog, candidate: ApplicationSettings
) -> None:
    """Persist one candidate before applying it and closing the editor."""
    try:
        self._settings_controller.commit(candidate)
    except SettingsStoreError as error:
        message = tr("Could not save settings: {p0}", p0=f"{error}")
        dialog.show_save_error(message)
        self.statusBar().showMessage(message)
        return
    dialog.accept()
    self.statusBar().showMessage(tr("Settings saved and applied."), CONFIRMATION_MESSAGE_TIMEOUT_MS)


def _show_about_dialog(self: MainWindow, _checked: bool = False) -> None:
    """Show the application identity and required GPL legal notice."""
    message_box = QMessageBox(self)
    message_box.setObjectName("aboutTermanaDialog")
    message_box.setWindowTitle(tr("About {p0}", p0=f"{APP_NAME}"))
    message_box.setIcon(QMessageBox.Icon.Information)
    message_box.setTextFormat(Qt.TextFormat.PlainText)
    message_box.setText(version_label())
    message_box.setInformativeText(
        support_details().split("\n", 1)[1]
        + "\n\n"
        + tr(
            "A free desktop terminal application.\n\nCopyright © 2026 Termana contributors.\nLicensed under the GNU General Public License, version 3 only.\nThis program comes with absolutely no warranty.\n\nSee the LICENSE file distributed with Termana or visit https://www.gnu.org/licenses/gpl-3.0.html for the complete terms."
        )
    )
    message_box.setStandardButtons(QMessageBox.StandardButton.Ok)
    copy_button = message_box.addButton(tr("Copy details"), QMessageBox.ButtonRole.ActionRole)
    copy_button.setObjectName("copyBuildDetailsButton")
    copy_button.setAutoDefault(False)
    copy_button.clicked.connect(lambda: QApplication.clipboard().setText(support_details()))
    try:
        message_box.exec()
    finally:
        schedule_delete_later(message_box)
