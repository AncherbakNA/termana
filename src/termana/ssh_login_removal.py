"""One chooser's confirmed removals; transactions remain window-owned."""

from uuid import UUID, uuid4

from PySide6.QtCore import QObject, Signal, Slot
from shiboken6 import isValid

from termana.credentials.ssh_auth import SshCredentialMutationOutcome
from termana.dialogs.ssh_login_dialog import SavedSshLoginOption
from termana.i18n import tr


class SshLoginRemovalController(QObject):
    status_message = Signal(str)

    def __init__(self, dialog, *, credentials, repository, coordinator, owner_current):
        super().__init__(dialog)
        self._dialog = dialog
        self._credentials = {item.credential_id: item for item in credentials}
        self._repository = repository
        self._coordinator = coordinator
        self._owner_current = owner_current
        self._pending = None
        self._stopped = False
        self.last_status = None
        dialog.remove_requested.connect(self.remove)
        coordinator.operation_finished.connect(self._finished)

    @property
    def credentials(self):
        return tuple(self._credentials.values())

    def _current(self):
        return (
            not self._stopped
            and isValid(self._dialog)
            and self._dialog.isVisible()
            and self._owner_current()
        )

    @Slot(object)
    def remove(self, credential_id):
        if not isinstance(credential_id, UUID) or self._pending is not None or not self._current():
            return
        credential = self._credentials.get(credential_id)
        if credential is None:
            return
        option = SavedSshLoginOption(credential.credential_id, credential.username)
        if not self._dialog.confirm_login_removal(option) or not self._current():
            return
        operation_id = uuid4()
        self._pending = (operation_id, credential_id)
        self.last_status = None
        self._dialog.set_removal_pending(True)
        try:
            self._coordinator.start_confirmed_login_removal(
                operation_id, self._repository, credential_id, credential.generation
            )
        except (RuntimeError, TypeError, ValueError):
            self._finished(operation_id, SshCredentialMutationOutcome.FAILED)

    @Slot(object, object)
    def _finished(self, operation_id, outcome):
        pending = self._pending
        if pending is None or operation_id != pending[0]:
            return
        self._pending = None
        if not self._current():
            if not self._stopped and isValid(self._dialog):
                self._dialog.reject()
            return
        self._dialog.set_removal_pending(False)
        if outcome in (
            SshCredentialMutationOutcome.REMOVE_SUCCEEDED,
            SshCredentialMutationOutcome.REMOVE_MISSING,
        ):
            self._credentials.pop(pending[1], None)
            self.last_status = (
                tr("Saved SSH login removed.")
                if outcome is SshCredentialMutationOutcome.REMOVE_SUCCEEDED
                else tr("That saved SSH login had already been removed.")
            )
            self.status_message.emit(self.last_status)
            if not self._dialog.forget_login(pending[1]):
                self._dialog.reject()
        elif outcome is SshCredentialMutationOutcome.REMOVE_GENERATION_CHANGED:
            self.last_status = tr(
                "This saved login changed and was not removed. Reopen the login chooser to review it."
            )
            self.status_message.emit(self.last_status)
            self._dialog.reject()
        else:
            self._dialog.show_operation_error(
                tr(
                    "Termana couldn't finish removing the saved SSH login. Unlock or check your system credential store, then try again."
                )
            )

    def stop(self):
        """Detach UI, never abandon an already authorized background transaction."""
        if self._stopped:
            return
        self._stopped = True
        self._coordinator.operation_finished.disconnect(self._finished)
