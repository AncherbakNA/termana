"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from uuid import UUID, uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from termana.app.background_operation import BackgroundOperation
from termana.app.qt_lifecycle import schedule_delete_later
from termana.app.window_contracts import (
    CONFIRMATION_MESSAGE_TIMEOUT_MS,
    LOGGER,
    PLACEHOLDER_MESSAGE_TIMEOUT_MS,
    _SshCredentialRemovalContext,
)
from termana.models.ssh_credential import SshCredentialRef
from termana.ssh.dialogs.saved_ssh_logins_dialog import SavedSshLoginsDialog
from termana.ssh.repository import SshCredentialRepository, SshCredentialRepositoryError
from termana.ui.i18n import tr


def _open_saved_ssh_logins_dialog(self: MainWindow, _checked: bool = False) -> None:
    """Open the metadata-only manager without unlocking the credential store."""
    if self._close_waiting_for_terminals:
        return
    repository = self._credential_repository
    if repository is None:
        self.statusBar().showMessage(
            tr("Saved SSH logins are unavailable."), PLACEHOLDER_MESSAGE_TIMEOUT_MS
        )
        return
    try:
        credentials = repository.saved_credentials()
    except SshCredentialRepositoryError:
        LOGGER.warning("Saved SSH login metadata could not be loaded")
        self._show_saved_ssh_logins_load_error()
        return
    dialog = SavedSshLoginsDialog(credentials=credentials, parent=self)
    dialog.remove_requested.connect(
        lambda credential_id: self._remove_saved_ssh_login_from_dialog(dialog, credential_id)
    )
    dialog.remove_all_requested.connect(
        lambda credential_ids: self._remove_saved_ssh_logins_from_dialog(dialog, credential_ids)
    )
    try:
        dialog.exec()
    finally:
        schedule_delete_later(dialog)


def _remove_saved_ssh_login_from_dialog(
    self: MainWindow, dialog: SavedSshLoginsDialog, credential_id: object
) -> None:
    """Start one opaque removal intent without blocking the GUI thread."""
    if self._close_waiting_for_terminals or not isinstance(credential_id, UUID):
        dialog.show_operation_error(tr("Termana couldn't identify the saved SSH login to remove."))
        return
    repository = self._credential_repository
    if repository is None:
        dialog.show_operation_error(tr("Saved SSH logins are unavailable right now."))
        return
    self._start_saved_ssh_login_removal(
        dialog=dialog,
        repository=repository,
        credential_ids=frozenset({credential_id}),
        is_single=True,
    )


def _remove_saved_ssh_logins_from_dialog(
    self: MainWindow, dialog: SavedSshLoginsDialog, credential_ids: object
) -> None:
    """Start one all-or-nothing batch removal outside the GUI thread."""
    if (
        self._close_waiting_for_terminals
        or not isinstance(credential_ids, frozenset)
        or (not credential_ids)
        or any(not isinstance(item, UUID) for item in credential_ids)
    ):
        dialog.show_operation_error(tr("Termana couldn't identify the saved SSH logins to remove."))
        return
    repository = self._credential_repository
    if repository is None:
        dialog.show_operation_error(tr("Saved SSH logins are unavailable right now."))
        return
    self._start_saved_ssh_login_removal(
        dialog=dialog, repository=repository, credential_ids=credential_ids, is_single=False
    )


def _start_saved_ssh_login_removal(
    self: MainWindow,
    *,
    dialog: SavedSshLoginsDialog,
    repository: SshCredentialRepository,
    credential_ids: frozenset[UUID],
    is_single: bool,
) -> None:
    """Own and dispatch one bounded worker while its modal dialog stays pending."""
    if self._ssh_credential_removal_operations:
        if not any(
            (
                context.dialog is dialog
                for _task, context in self._ssh_credential_removal_operations.values()
            )
        ):
            dialog.show_operation_error(
                tr("Another saved SSH login change is already in progress.")
            )
        return
    dialog.clear_operation_error()
    self.saved_ssh_logins_action.setEnabled(False)
    operation_id = uuid4()
    if is_single:
        credential_id = next(iter(credential_ids))

        def operation() -> object:
            return repository.remove_credential(credential_id)
    else:

        def operation() -> object:
            return repository.remove_credentials(credential_ids)

    task = BackgroundOperation(operation_id, operation)
    context = _SshCredentialRemovalContext(
        dialog=dialog, repository=repository, credential_ids=credential_ids, is_single=is_single
    )
    self._ssh_credential_removal_operations[operation_id] = (task, context)
    task.signals.succeeded.connect(self._handle_saved_ssh_login_removal_succeeded)
    task.signals.failed.connect(self._handle_saved_ssh_login_removal_failed)
    try:
        self._credential_operation_starter(task)
    except Exception:
        self._ssh_credential_removal_operations.pop(operation_id, None)
        self._update_saved_ssh_logins_action()
        LOGGER.warning("The saved SSH login background operation could not start")
        self._show_saved_ssh_login_removal_error(dialog)


def _handle_saved_ssh_login_removal_succeeded(
    self: MainWindow, operation_id: object, result: object
) -> None:
    """Refresh metadata and release a pending dialog after a worker commit."""
    entry = self._take_saved_ssh_login_removal(operation_id)
    if entry is None:
        return
    _task, context = entry
    if context.is_single:
        if not isinstance(result, bool):
            LOGGER.warning("A saved SSH login removal returned an invalid result")
            self._show_saved_ssh_login_removal_error(context.dialog)
            self._resume_close_after_saved_ssh_login_removal(context.dialog)
            return
        removed_count = int(result)
    else:
        if not isinstance(result, tuple) or any(
            not isinstance(credential, SshCredentialRef) for credential in result
        ):
            LOGGER.warning("A saved SSH login batch removal returned an invalid result")
            self._show_saved_ssh_login_removal_error(context.dialog)
            self._resume_close_after_saved_ssh_login_removal(context.dialog)
            return
        returned_ids = frozenset(credential.credential_id for credential in result)
        if len(returned_ids) != len(result) or not returned_ids <= context.credential_ids:
            LOGGER.warning("A saved SSH login batch removal returned inconsistent metadata")
            self._show_saved_ssh_login_removal_error(context.dialog)
            self._resume_close_after_saved_ssh_login_removal(context.dialog)
            return
        removed_count = len(result)
    if not self._refresh_saved_ssh_logins_dialog(context.dialog, context.repository):
        self._resume_close_after_saved_ssh_login_removal(context.dialog)
        return
    requested_count = len(context.credential_ids)
    if context.is_single:
        message = (
            tr("Saved SSH login removed.")
            if removed_count
            else tr("That saved SSH login had already been removed.")
        )
    elif removed_count == requested_count:
        message = (
            tr("Saved SSH login removed.")
            if removed_count == 1
            else tr("{p0} saved SSH logins removed.", p0=f"{removed_count}")
        )
    elif removed_count == 0:
        message = tr("Those saved SSH logins had already been removed.")
    else:
        removed_label = tr("saved SSH login") if removed_count == 1 else tr("saved SSH logins")
        missing_count = requested_count - removed_count
        missing_label = (
            tr("saved SSH login was") if missing_count == 1 else tr("saved SSH logins were")
        )
        message = tr(
            "{p0} {p1} removed; {p2} {p3} already absent.",
            p0=f"{removed_count}",
            p1=f"{removed_label}",
            p2=f"{missing_count}",
            p3=f"{missing_label}",
        )
    self.statusBar().showMessage(message, CONFIRMATION_MESSAGE_TIMEOUT_MS)
    self._resume_close_after_saved_ssh_login_removal(context.dialog)


def _handle_saved_ssh_login_removal_failed(self: MainWindow, operation_id: object) -> None:
    """Release a pending dialog after a private worker failure."""
    entry = self._take_saved_ssh_login_removal(operation_id)
    if entry is None:
        return
    _task, context = entry
    LOGGER.warning("A saved SSH login background removal failed")
    self._show_saved_ssh_login_removal_error(context.dialog)
    self._resume_close_after_saved_ssh_login_removal(context.dialog)


def _take_saved_ssh_login_removal(
    self: MainWindow, operation_id: object
) -> tuple[BackgroundOperation, _SshCredentialRemovalContext] | None:
    """Consume exactly one queued completion identified by an opaque UUID."""
    if not isinstance(operation_id, UUID):
        LOGGER.warning("A saved SSH login operation returned an invalid identity")
        return None
    entry = self._ssh_credential_removal_operations.pop(operation_id, None)
    self._update_saved_ssh_logins_action()
    return entry


def _update_saved_ssh_logins_action(self: MainWindow) -> None:
    """Expose the manager only when its repository and worker are available."""
    self.saved_ssh_logins_action.setEnabled(
        self._credential_repository is not None
        and (not self._ssh_credential_removal_operations)
        and (not self._close_waiting_for_terminals)
    )


def _refresh_saved_ssh_logins_dialog(
    self: MainWindow, dialog: SavedSshLoginsDialog, repository: SshCredentialRepository
) -> bool:
    """Refresh only non-secret rows after a successful repository commit."""
    try:
        credentials = repository.saved_credentials()
    except SshCredentialRepositoryError:
        LOGGER.warning("Saved SSH login metadata could not be refreshed")
        dialog.show_operation_error(
            tr(
                "The change was saved, but the list couldn't be refreshed. Close and reopen this window to try again."
            )
        )
        return False
    dialog.set_credentials(credentials)
    return True


def _show_saved_ssh_login_removal_error(self: MainWindow, dialog: SavedSshLoginsDialog) -> None:
    """Report a retryable credential-store failure without exposing internals."""
    message = tr(
        "Termana couldn't finish removing the saved SSH login. Unlock or check your system credential store, then try again."
    )
    dialog.show_operation_error(message)
    self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)


def _show_saved_ssh_logins_load_error(self: MainWindow) -> None:
    """Distinguish an unreadable metadata index from a genuinely empty list."""
    message_box = QMessageBox(self)
    message_box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    message_box.setObjectName("savedSshLoginsLoadErrorDialog")
    message_box.setWindowTitle(tr("Saved SSH logins unavailable"))
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setTextFormat(Qt.TextFormat.PlainText)
    message_box.setText(tr("Termana couldn't load the saved SSH login list."))
    message_box.setInformativeText(
        tr("Your saved passwords were not read or changed. Close this message and try again.")
    )
    message_box.setStandardButtons(QMessageBox.StandardButton.Ok)
    try:
        message_box.exec()
    finally:
        schedule_delete_later(message_box)


def _ssh_account_choices(self: MainWindow):
    repository = self._credential_repository
    if repository is None:
        return ()
    try:
        return repository.accounts()
    except SshCredentialRepositoryError:
        self.statusBar().showMessage(
            "SSH accounts are unavailable; existing bindings are retained."
        )
        return ()


def _manage_ssh_accounts(self: MainWindow, _checked=False):
    from termana.ssh.dialogs.ssh_accounts_dialog import SshAccountsDialog

    if self._credential_repository is None or self._close_waiting_for_terminals:
        return None
    if self._ssh_account_dialog is not None:
        self._ssh_account_dialog.raise_()
        self._ssh_account_dialog.activateWindow()
        return None
    dialog = None
    try:
        dialog = SshAccountsDialog(
            self._credential_repository, self._ssh_credential_mutation_coordinator, self
        )
        self._ssh_account_dialog = dialog
        dialog.exec()
    except SshCredentialRepositoryError:
        self.statusBar().showMessage("SSH accounts could not be loaded.")
    finally:
        self._ssh_account_dialog = None
        if dialog is not None:
            schedule_delete_later(dialog)
    return self._ssh_account_choices()
