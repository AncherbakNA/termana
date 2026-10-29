"""Persist connection drafts before closing their editor, not after acceptance."""

from collections.abc import Callable
from uuid import UUID

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from termana.dialogs.connection_dialog import ConnectionDialog
from termana.i18n import tr
from termana.models.connection import ConnectionCatalog, ConnectionProfile
from termana.models.settings import SshEngine
from termana.models.ssh_jump import SshJumpMode
from termana.qt_lifecycle import schedule_delete_later


class ConnectionProfileEditor(QObject):
    """Own one modal draft, retaining it when persistence or stale-state checks fail."""

    message = Signal(str)

    def __init__(
        self,
        window: QWidget,
        *,
        catalog: Callable[[], ConnectionCatalog],
        available: Callable[[], bool],
        scope: Callable[[], object],
        commit: Callable[[ConnectionCatalog, UUID], bool],
        new_route: Callable[[], SshJumpMode],
        accounts: Callable = lambda: (),
        manage_accounts: Callable | None = None,
        ssh_engine: Callable[[], SshEngine] = lambda: SshEngine.SYSTEM,
    ) -> None:
        super().__init__(window)
        self._window, self._catalog, self._available = window, catalog, available
        self._scope, self._commit, self._new_route = scope, commit, new_route
        self._accounts, self._manage_accounts = accounts, manage_accounts
        self._ssh_engine = ssh_engine
        self._editor: ConnectionDialog | None = None
        self._original: ConnectionProfile | None = None
        self._original_folder_id: UUID | None = None
        self._store_scope: object = None
        self._closing = False
        self._saving = False

    def create(self, folder_id: UUID | None = None, *, initial_protocol: str | None = None) -> None:
        self._open(None, folder_id, initial_protocol=initial_protocol)

    def edit(self, profile: ConnectionProfile) -> None:
        if not isinstance(profile, ConnectionProfile):
            return
        catalog = self._catalog()
        current = catalog.profile_by_id(profile.profile_id)
        if current is None:
            self.message.emit(tr("Could not find the connection to update."))
            return
        self._open(current, catalog.parent_id_of(current.profile_id))

    def _open(self, original: ConnectionProfile | None, folder_id: UUID | None,
              *, initial_protocol: str | None = None) -> None:
        if self._closing or not self._available():
            return
        if self._editor is not None:
            self._editor.raise_()
            self._editor.activateWindow()
            return
        self._original, self._store_scope = original, self._scope()
        self._original_folder_id = folder_id
        dialog = None
        try:
            dialog = ConnectionDialog(
                self._window,
                profile=original,
                folder_choices=self._catalog().folder_choices(),
                initial_folder_id=folder_id,
                new_ssh_route=self._new_route(),
                save_handler=self._save,
                account_choices=self._accounts(),
                manage_accounts=self._manage_accounts,
                ssh_engine=self._ssh_engine(),
                initial_protocol=initial_protocol,
            )
            self._editor = dialog
            dialog.exec()
        finally:
            self._editor, self._original, self._store_scope = None, None, None
            self._original_folder_id = None
            if dialog is not None:
                schedule_delete_later(dialog)

    def _save(self, profile: ConnectionProfile, folder_id: UUID | None) -> bool:
        if (
            self._saving
            or self._closing
            or self._editor is None
            or not self._available()
            or self._scope() is not self._store_scope
            or type(profile) is not ConnectionProfile
        ):
            return False
        self._saving = True
        try:
            catalog, original = self._catalog(), self._original
            if original is None:
                candidate = catalog.with_profile_added(profile, parent_folder_id=folder_id)
                message = tr('Connection "{p0}" saved.', p0=profile.name)
            else:
                # A nested event loop can outlive import/reload or deletion.
                # Never replace a newer record with this older form snapshot.
                if profile.profile_id != original.profile_id:
                    self.message.emit(tr("Edited connection returned an unexpected ID."))
                    return False
                if (
                    catalog.profile_by_id(original.profile_id) != original
                    or catalog.parent_id_of(original.profile_id) != self._original_folder_id
                ):
                    self.message.emit(
                        tr(
                            "The connection changed while its editor was open. Your draft was not saved."
                        )
                    )
                    return False
                candidate = catalog.with_profile_updated(profile)
                if folder_id != catalog.parent_id_of(original.profile_id):
                    candidate = candidate.with_item_moved(
                        profile.profile_id, parent_folder_id=folder_id
                    )
                message = tr('Connection "{p0}" updated.', p0=profile.name)
            if not self._commit(candidate, profile.profile_id):
                return False
            self.message.emit(message)
            return True
        except ValueError as error:
            self.message.emit(tr("Could not update connection: {p0}", p0=str(error)))
            return False
        finally:
            self._saving = False

    def stop(self) -> None:
        self._closing = True
        if isinstance(self._editor, ConnectionDialog) and isValid(self._editor):
            self._editor.reject()
