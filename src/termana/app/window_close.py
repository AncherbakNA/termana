"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog
from shiboken6 import isValid

from termana.app.qt_lifecycle import schedule_delete_later
from termana.app.window_contracts import _ConnectionTransferKind
from termana.ssh.dialogs.saved_ssh_logins_dialog import SavedSshLoginsDialog
from termana.ui.dialogs.action_dialog import ActionDialog
from termana.ui.i18n import tr


def _handle_ssh_credential_mutation_pending_count_changed(self: MainWindow, count: int) -> None:
    """Resume an application close after every authorized keychain write settles."""
    if count == 0 and self._close_waiting_for_terminals and self._terminal_shutdown_complete:
        QTimer.singleShot(0, self.close)


def _resume_close_after_ssh_repository_operations(self: MainWindow) -> None:
    """Hold instance ownership until every possible index writer has finished."""
    if (
        self._close_waiting_for_terminals
        and (not self._ssh_preference_operations)
        and (not self._ssh_login_resolution_operations)
    ):
        QTimer.singleShot(0, self.close)


def _rdp_pending_count_changed(self: MainWindow, count):
    if count == 0:
        self._resume_close_after_ssh_repository_operations()


def _resume_close_after_saved_ssh_login_removal(
    self: MainWindow, dialog: SavedSshLoginsDialog
) -> None:
    """Let an application close continue only after its keychain transaction ends."""
    if not self._close_waiting_for_terminals:
        return
    dialog.reject()
    if not self._ssh_credential_removal_operations:
        QTimer.singleShot(0, self.close)


def closeEvent(self: MainWindow, event: QCloseEvent, *, _base_close_event) -> None:
    """Keep Qt alive while terminal and known_hosts helpers stop."""
    sync = getattr(self, "_sync_dialog", None)
    if sync is not None and sync.pending is not None:
        sync.reject()
        self.statusBar().showMessage(
            "Finishing GitHub synchronization before closing. Try closing again when it finishes."
        )
        event.ignore()
        return
    if sync is not None:
        sync.reject()
    if not self._close_waiting_for_terminals and (not self._application_close_confirmed):
        active_terminal_count = self.terminal_workspace.active_terminal_count
        if active_terminal_count and self.isVisible():
            if not self._confirm_application_close(active_terminal_count):
                event.ignore()
                return
        self._application_close_confirmed = True
    update_dialog = getattr(self, "_update_dialog", None)
    if update_dialog is not None:
        update_dialog.stop_automatic_checks()
        update_dialog.reject()
    self._persist_window_layout_once()
    self._close_waiting_for_terminals = True
    if self._ssh_account_dialog is not None:
        self._ssh_account_dialog.reject()
    if self._private_recovery_dialog is not None and isValid(self._private_recovery_dialog):
        self._private_recovery_dialog.close()
    if self._connection_transfer_dialog is not None:
        self._connection_transfer_dialog.reject()
    for dialog in tuple(self._ssh_login_dialogs.values()):
        dialog.reject()
    for dialog in tuple(self._ssh_host_key_dialogs):
        dialog.reject()
    if self._ssh_tunnel_launch_dialog is not None:
        self._ssh_tunnel_launch_dialog.reject()
    self._terminal_output_controller.stop_accepting_exports()
    self._ssh_config_controller.stop()
    self._gateway_import_controller.stop()
    self._group_input_controller.stop()
    self._command_snippet_controller.stop()
    self._open_terminals_controller.stop()
    self._terminal_multi_search_controller.stop()
    self._ssh_session_details_controller.stop()
    self._sftp_controller.stop()
    self._ssh_session_save_controller.stop()
    self._connection_profile_editor.stop()
    self._update_connection_transfer_actions()
    self.terminal_workspace.suspend_shortcut_fallback()
    active_ssh_lookup = self._active_ssh_login_resolution()
    if active_ssh_lookup is not None:
        self._abandon_ssh_login_resolution(active_ssh_lookup)
    self._set_ssh_login_lookup_pending(None)
    self.setEnabled(False)
    if self._ssh_credential_removal_operations:
        self.statusBar().showMessage(tr("Finishing the saved SSH login change before closing…"))
        event.ignore()
        return
    if not self._terminal_shutdown_complete:
        if not self.terminal_workspace.shutdown_all():
            if self._known_hosts_updater.is_running:
                self._known_hosts_updater.cancel()
            event.ignore()
            return
        self._terminal_shutdown_complete = True
    transfer_operation = self._connection_transfer_operation
    if transfer_operation is not None:
        message = (
            tr("Finishing the connection file check before closing…")
            if transfer_operation.kind is _ConnectionTransferKind.IMPORT
            else tr("Finishing the imported connection save before closing…")
            if transfer_operation.kind is _ConnectionTransferKind.IMPORT_COMMIT
            else tr("Finishing the confirmed connection export before closing…")
        )
        self.statusBar().showMessage(message)
        event.ignore()
        return
    if self._rdp_identity_repository is not None and self._rdp_identity_repository.is_busy:
        self.statusBar().showMessage(tr("Finishing the RDP system-vault request before closing…"))
        event.ignore()
        return
    if self._ssh_credential_mutation_coordinator.has_pending_operations:
        self.statusBar().showMessage(
            tr("Finishing the secure SSH credential change before closing…")
        )
        event.ignore()
        return
    if self._ssh_preference_operations or self._ssh_login_resolution_operations:
        self.statusBar().showMessage(
            tr("Finishing the saved SSH login check or preference change…")
        )
        event.ignore()
        return
    if self._terminal_output_controller.is_busy:
        self.statusBar().showMessage(tr("Finishing the terminal output export before closing…"))
        event.ignore()
        return
    if self._known_hosts_updater.is_running:
        self._known_hosts_updater.cancel()
        if self._known_hosts_updater.is_running:
            event.ignore()
            return
    update_job = getattr(self, "_update_install_job", None)
    if update_job is not None:
        try:
            from termana.app.paths import application_config_directory
            from termana.storage.configuration_backup import create_backup
            from termana.updates.installer import launch_installer

            create_backup(application_config_directory(), update_job / "configuration-backup.zip")
            self._update_installer_process = launch_installer(update_job)
            self._update_install_job = None
        except Exception:
            self._update_install_job = None
            self.statusBar().showMessage(
                "Update could not be handed off securely. The current app was not replaced; restart it normally and retry."
            )
            event.ignore()
            return
    _base_close_event(event)


def _confirm_application_close(self: MainWindow, active_terminal_count: int) -> bool:
    """Ask once before closing live terminal, file, desktop or database sessions."""
    if type(active_terminal_count) is not int or active_terminal_count <= 0:
        raise ValueError("active_terminal_count must be a positive integer")
    noun = tr("session") if active_terminal_count == 1 else tr("sessions")
    dialog = ActionDialog(
        object_name="closeApplicationDialog",
        window_title=tr("Close Termana?"),
        title=tr("Close {p0} active {p1}?", p0=f"{active_terminal_count}", p1=f"{noun}"),
        subtitle=tr("Active sessions are still running"),
        body=tr("Closing Termana disconnects these sessions. Running work may be interrupted and uncommitted database changes may be lost."),
        primary_text=tr("Close {p0}", p0=f"{noun}"),
        primary_object_name="confirmCloseApplicationButton",
        secondary_text=tr("Keep open"),
        secondary_object_name="cancelCloseApplicationButton",
        tone="notice",
        primary_role="danger",
        badge_text="!",
        parent=self,
    )
    dialog.badge_symbol.setAccessibleName(tr("Active sessions notice"))
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        schedule_delete_later(dialog)


def _finish_close_after_terminal_shutdown(self: MainWindow) -> None:
    """Retry a pending window close after the final backend has stopped."""
    if not self._close_waiting_for_terminals:
        return
    if not self.terminal_workspace.shutdown_all():
        return
    self._terminal_shutdown_complete = True
    self.close()
