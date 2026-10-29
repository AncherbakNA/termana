"""Create a new local profile from a reviewed, exact SSH launch snapshot."""

from collections.abc import Callable
from dataclasses import replace
from unicodedata import normalize
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from termana.dialogs.connection_dialog import ConnectionDialog
from termana.i18n import tr
from termana.models.connection import (
    MAX_CONNECTION_NAME_CHARACTERS,
    ConnectionCatalog,
    ConnectionProfile,
    ConnectionProtocol,
    connection_profile_copy_name,
)
from termana.models.settings import SshEngine
from termana.qt_lifecycle import schedule_delete_later
from termana.widgets.terminal_workspace import TerminalWorkspace


def session_profile_template(
    source: ConnectionProfile, catalog: ConnectionCatalog
) -> ConnectionProfile:
    """Drop the destination login, favorite mark and profile ID; retain local SSH options."""
    if type(source) is not ConnectionProfile or source.protocol is not ConnectionProtocol.SSH:
        raise ValueError("Only launched SSH profiles can be saved")
    # A Quick Connect title may contain the entered username; do not use it as
    # an automatically persisted name. Existing profile names are user-authored.
    base = source.name if catalog.profile_by_id(source.profile_id) is not None else source.host
    base = normalize("NFC", base)[:MAX_CONNECTION_NAME_CHARACTERS].rstrip()
    reserved = tuple(item.name for item in (*catalog.profiles, *catalog.folders))
    name = (
        connection_profile_copy_name(base, reserved)
        if normalize("NFC", base).casefold() in {normalize("NFC", n).casefold() for n in reserved}
        else base
    )
    return replace(source, name=name, profile_id=uuid4(), username=None,
                   ssh_account_id=None, is_favorite=False)


class SshSessionSaveController(QObject):
    """Retain the draft on failed persistence; never overwrite or reconnect a source."""

    message = Signal(str)

    def __init__(
        self,
        window: QWidget,
        workspace: TerminalWorkspace,
        *,
        catalog: Callable[[], ConnectionCatalog],
        available: Callable[[], bool],
        scope: Callable[[], object],
        persist: Callable[[ConnectionProfile, UUID | None], bool],
        ssh_engine: Callable[[], SshEngine] = lambda: SshEngine.SYSTEM,
    ) -> None:
        super().__init__(window)
        self._window, self._workspace = window, workspace
        self._ssh_engine = ssh_engine
        self._catalog, self._available, self._scope, self._persist = (
            catalog,
            available,
            scope,
            persist,
        )
        self._editor: ConnectionDialog | None = None
        self._session_id: UUID | None = None
        self._source: ConnectionProfile | None = None
        self._store_scope: object = None
        self._closing = False
        self._saving = False
        workspace.save_ssh_session_requested.connect(self.open_session)
        workspace.open_sessions_changed.connect(self._check_owner)

    def open_dialog(self, _checked: bool = False) -> None:
        self.open_session(self._workspace.current_ssh_session_id)

    def open_session(self, session_id: object) -> None:
        if self._closing or not isinstance(session_id, UUID):
            return
        if self._editor is not None:
            self._editor.raise_()
            self._editor.activateWindow()
            return
        source = self._workspace.ssh_session_profile(session_id)
        if source is None:
            return
        if not self._available():
            self.message.emit(
                tr("Saving profiles is unavailable until the connection file is ready.")
            )
            return
        catalog = self._catalog()
        self._session_id, self._source, self._store_scope = session_id, source, self._scope()
        dialog = None
        try:
            dialog = ConnectionDialog(
                self._window,
                initial_profile=session_profile_template(source, catalog),
                folder_choices=catalog.folder_choices(),
                initial_folder_id=(
                    catalog.parent_id_of(source.profile_id)
                    if catalog.profile_by_id(source.profile_id) is not None
                    else None
                ),
                save_handler=self._save,
                ssh_engine=self._ssh_engine(),
            )
            dialog.setWindowTitle(tr("Save SSH session as profile"))
            dialog.setObjectName("saveSshSessionDialog")
            dialog.protocol_input.setEnabled(False)
            self._editor = dialog
            if self._valid_owner():
                dialog.exec()
        finally:
            self._editor = None
            self._session_id = None
            self._source = None
            self._store_scope = None
            if dialog is not None:
                schedule_delete_later(dialog)

    def _valid_owner(self) -> bool:
        return (
            not self._closing
            and self._source is not None
            and self._workspace.ssh_session_profile(self._session_id) is self._source
            and self._scope() is self._store_scope
        )

    def _save(self, candidate: ConnectionProfile, folder_id: UUID | None) -> bool:
        if self._saving or self._editor is None or not self._valid_owner() or not self._available():
            return False
        if (
            candidate.protocol is not ConnectionProtocol.SSH
            or candidate.username is not None
            or candidate.is_favorite
            or candidate.profile_id == self._source.profile_id
            or self._catalog().profile_by_id(candidate.profile_id) is not None
        ):
            return False
        self._saving = True
        try:
            if not self._persist(candidate, folder_id):
                return False
            self.message.emit(
                tr('Connection "{p0}" saved. The SSH session is unchanged.', p0=f"{candidate.name}")
            )
            return not self._closing
        finally:
            self._saving = False

    def _check_owner(self) -> None:
        if self._editor is not None and isValid(self._editor) and not self._valid_owner():
            self._editor.reject()

    def stop(self) -> None:
        self._closing = True
        if self._editor is not None and isValid(self._editor):
            self._editor.reject()
