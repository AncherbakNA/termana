"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from dataclasses import replace
from uuid import UUID

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton

from termana.app.qt_lifecycle import schedule_delete_later
from termana.app.window_contracts import (
    CONFIRMATION_MESSAGE_TIMEOUT_MS,
    LOGGER,
    _ConnectionOpenState,
    _ConnectionTransferKind,
)
from termana.connections.dialogs.connection_folder_dialog import ConnectionFolderDialog
from termana.models.connection import (
    ConnectionCatalog,
    ConnectionFolder,
    ConnectionProfile,
    ConnectionProtocol,
    connection_profile_copy_name,
)
from termana.storage.connection_store import ConnectionStoreError
from termana.ui.i18n import tr


def _open_connection(self: MainWindow, profile: object) -> _ConnectionOpenState:
    """Dispatch a validated saved profile to its isolated transport path."""
    if not isinstance(profile, ConnectionProfile):
        self.statusBar().showMessage(tr("Could not open an invalid connection profile."))
        return _ConnectionOpenState.REJECTED
    if profile.protocol is ConnectionProtocol.SSH:
        return self._open_ssh_connection(profile)
    if profile.protocol is ConnectionProtocol.TELNET:
        return (
            _ConnectionOpenState.ACCEPTED
            if self._open_telnet_connection(profile)
            else _ConnectionOpenState.REJECTED
        )
    if profile.protocol is ConnectionProtocol.RDP:
        from termana.rdp.ui import open_saved_rdp_profile

        if self._connection_catalog.profile_by_id(profile.profile_id) != profile:
            self.statusBar().showMessage(
                tr("This connection changed. Open it from the current list.")
            )
            return _ConnectionOpenState.REJECTED
        return (
            _ConnectionOpenState.ACCEPTED
            if open_saved_rdp_profile(self, profile)
            else _ConnectionOpenState.REJECTED
        )
    self.statusBar().showMessage(tr("This connection protocol is not supported yet."))
    return _ConnectionOpenState.REJECTED


def _open_new_connection_dialog(self: MainWindow, _checked: bool = False) -> None:
    """Open a profile form targeting the User sessions root."""
    self._open_new_connection_in_folder(None)


def _open_new_connection_in_folder(self: MainWindow, destination: object) -> None:
    """Open the profile form for one validated catalog destination."""
    if self._close_waiting_for_terminals:
        return
    is_valid, folder_id = self._resolve_connection_folder_id(destination)
    if not is_valid:
        return
    self._connection_profile_editor.create(folder_id)


def _load_connection_catalog(self: MainWindow) -> None:
    """Load the stored tree without allowing a bad file to crash the UI."""
    active_transfer = self._connection_transfer_operation
    if (
        active_transfer is not None
        and active_transfer.kind is _ConnectionTransferKind.IMPORT_COMMIT
    ):
        self.statusBar().showMessage(tr("Wait for the imported connections to finish saving."))
        return
    if self._connection_store is None:
        return
    try:
        catalog = self._connection_store.load()
    except ConnectionStoreError as error:
        self._connection_store_ready = False
        self.connections_sidebar.set_catalog_available(False)
        self.new_connection_action.setEnabled(False)
        self.new_folder_action.setEnabled(False)
        self._update_connection_transfer_actions()
        self.statusBar().showMessage(tr("Could not load connections: {p0}", p0=f"{error}"))
        return
    self._connection_store_ready = True
    self._connection_catalog = catalog
    self.connections_sidebar.set_catalog(catalog)
    self.connections_sidebar.set_catalog_available(True)
    self.new_connection_action.setEnabled(True)
    self.new_folder_action.setEnabled(True)
    self._update_connection_transfer_actions()
    if catalog.profiles:
        self.statusBar().showMessage(
            tr("Loaded {p0} saved connection(s).", p0=f"{len(catalog.profiles)}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )


def _ssh_session_save_available(self: MainWindow) -> bool:
    operation = self._connection_transfer_operation
    return (
        not self._close_waiting_for_terminals
        and (self._connection_store is None or self._connection_store_ready)
        and (
            not (operation is not None and operation.kind is _ConnectionTransferKind.IMPORT_COMMIT)
        )
    )


def _add_connection_profile(
    self: MainWindow, profile: ConnectionProfile, *, parent_folder_id: UUID | None = None
) -> bool:
    """Persist a profile first, then expose it through the interface."""
    try:
        candidate = self._connection_catalog.with_profile_added(
            profile, parent_folder_id=parent_folder_id
        )
    except ValueError as error:
        self.statusBar().showMessage(tr("Could not add connection: {p0}", p0=f"{error}"))
        return False
    return self._commit_connection_catalog(candidate, select_item_id=profile.profile_id)


def _edit_connection_profile(self: MainWindow, profile: ConnectionProfile) -> None:
    """Edit a profile while preserving its identity and stored position."""
    if self._close_waiting_for_terminals:
        return
    self._connection_profile_editor.edit(profile)


def _set_connection_favorite(self: MainWindow, profile_id: object, is_favorite: bool) -> None:
    """Persist a favorite mark against the current profile, never a stale row."""
    if (
        self._close_waiting_for_terminals
        or not isinstance(profile_id, UUID)
        or type(is_favorite) is not bool
    ):
        return
    profile = self._connection_catalog.profile_by_id(profile_id)
    if profile is None:
        self.statusBar().showMessage(tr("Could not find the connection to update."))
        return
    if profile.is_favorite == is_favorite:
        return
    candidate = self._connection_catalog.with_profile_updated(
        replace(profile, is_favorite=is_favorite)
    )
    if self._commit_connection_catalog(candidate):
        message = (
            tr("Connection added to favorites.")
            if is_favorite
            else tr("Connection removed from favorites.")
        )
        self.statusBar().showMessage(message, CONFIRMATION_MESSAGE_TIMEOUT_MS)


def _duplicate_connection_profile(self: MainWindow, profile: object) -> None:
    """Persist a credential-free copy directly after one current saved profile."""
    if self._close_waiting_for_terminals or not isinstance(profile, ConnectionProfile):
        return
    current_profile = self._connection_catalog.profile_by_id(profile.profile_id)
    if current_profile is None:
        self.statusBar().showMessage(tr("Could not find the connection to duplicate."))
        return
    try:
        parent_folder_id = self._connection_catalog.parent_id_of(current_profile.profile_id)
        siblings = self._connection_catalog.children_of(parent_folder_id)
        source_index = next(
            (
                index
                for index, sibling in enumerate(siblings)
                if isinstance(sibling, ConnectionProfile)
                and sibling.profile_id == current_profile.profile_id
            )
        )
        duplicate_name = connection_profile_copy_name(
            current_profile.name, (sibling.name for sibling in siblings)
        )
        duplicate = ConnectionProfile(
            name=duplicate_name,
            host=current_profile.host,
            protocol=current_profile.protocol,
            port=current_profile.port,
            telnet_encoding=current_profile.telnet_encoding,
            word_delete_mode=current_profile.word_delete_mode,
            accent_color=current_profile.accent_color,
            ssh_cipher=current_profile.ssh_cipher,
            ssh_algorithms=current_profile.ssh_algorithms,
            ssh_options=current_profile.ssh_options,
            ssh_identity=current_profile.ssh_identity,
            ssh_jump=current_profile.ssh_jump,
            ssh_tunnels=current_profile.ssh_tunnels,
            rdp_options=current_profile.rdp_options.without_login(),
            is_favorite=False,
            username=None,
        )
        candidate = self._connection_catalog.with_profile_added(
            duplicate, parent_folder_id=parent_folder_id, index=source_index + 1
        )
    except (StopIteration, ValueError) as error:
        LOGGER.warning("Could not prepare connection duplicate: %s", error)
        self.statusBar().showMessage(tr("Could not duplicate this connection."))
        return
    if not self._commit_connection_catalog(candidate, select_item_id=duplicate.profile_id):
        return
    if not self.connections_sidebar.catalog_item_is_visible(duplicate.profile_id):
        self.statusBar().showMessage(
            tr(
                'Connection "{p0}" duplicated. Clear the session filter to see it.',
                p0=f"{duplicate.name}",
            ),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )
        return
    self.statusBar().showMessage(
        tr('Connection "{p0}" duplicated.', p0=f"{duplicate.name}"), CONFIRMATION_MESSAGE_TIMEOUT_MS
    )


def _delete_connection_profile(self: MainWindow, profile: ConnectionProfile) -> None:
    """Delete a profile only after explicit confirmation and persistence."""
    if self._close_waiting_for_terminals:
        return
    current_profile = self._connection_catalog.profile_by_id(profile.profile_id)
    if current_profile is None:
        self.statusBar().showMessage(tr("Could not find the connection to delete."))
        return
    if not self._confirm_connection_deletion(current_profile):
        return
    parent_folder_id = self._connection_catalog.parent_id_of(current_profile.profile_id)
    try:
        candidate = self._connection_catalog.without_profile(current_profile.profile_id)
    except ValueError as error:
        self.statusBar().showMessage(tr("Could not delete connection: {p0}", p0=f"{error}"))
        return
    if not self._commit_connection_catalog(candidate, select_item_id=parent_folder_id):
        return
    self.statusBar().showMessage(
        tr('Connection "{p0}" deleted.', p0=f"{current_profile.name}"),
        CONFIRMATION_MESSAGE_TIMEOUT_MS,
    )


def _commit_connection_catalog(
    self: MainWindow, candidate: ConnectionCatalog, *, select_item_id: UUID | None = None
) -> bool:
    """Persist a complete candidate before changing memory or the tree."""
    if not isinstance(candidate, ConnectionCatalog):
        raise TypeError("candidate must be a ConnectionCatalog")
    if self._close_waiting_for_terminals:
        return False
    active_transfer = self._connection_transfer_operation
    if (
        active_transfer is not None
        and active_transfer.kind is _ConnectionTransferKind.IMPORT_COMMIT
    ):
        self.statusBar().showMessage(tr("Wait for the imported connections to finish saving."))
        return False
    if self._connection_store is not None:
        if not self._connection_store_ready:
            self.statusBar().showMessage(
                tr("Saving is disabled because the connection file could not be loaded.")
            )
            return False
        try:
            self._connection_store.save(candidate)
        except ConnectionStoreError as error:
            self.statusBar().showMessage(tr("Could not save connections: {p0}", p0=f"{error}"))
            return False
    self._apply_connection_catalog(candidate, select_item_id=select_item_id)
    return True


def _apply_connection_catalog(
    self: MainWindow, candidate: ConnectionCatalog, *, select_item_id: UUID | None = None
) -> None:
    """Publish a catalog only after its persistence transaction has succeeded."""
    if not isinstance(candidate, ConnectionCatalog):
        raise TypeError("candidate must be a ConnectionCatalog")
    if self._rdp_identity_repository is not None:
        self._rdp_identity_repository.catalog_changed(self._connection_catalog, candidate)
    self._connection_catalog = candidate
    self.terminal_workspace.update_connection_colors(candidate.profiles)
    self.connections_sidebar.set_catalog(candidate, select_item_id=select_item_id)
    self._update_connection_transfer_actions()


def _open_new_connection_folder(self: MainWindow, _checked: bool = False) -> None:
    """Open a folder form targeting the User sessions root."""
    self._open_new_connection_folder_in_parent(None)


def _open_new_connection_folder_in_parent(self: MainWindow, parent: object) -> None:
    """Create one empty folder below a validated root or folder."""
    if self._close_waiting_for_terminals:
        return
    is_valid, parent_folder_id = self._resolve_connection_folder_id(parent)
    if not is_valid:
        return
    dialog = ConnectionFolderDialog(
        self, reserved_names=self._sibling_connection_folder_names(parent_folder_id)
    )
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        folder = dialog.connection_folder
    finally:
        schedule_delete_later(dialog)
    try:
        candidate = self._connection_catalog.with_folder_added(
            folder, parent_folder_id=parent_folder_id
        )
    except ValueError as error:
        self.statusBar().showMessage(tr("Could not create folder: {p0}", p0=f"{error}"))
        return
    if not self._commit_connection_catalog(candidate, select_item_id=folder.folder_id):
        return
    self.statusBar().showMessage(
        tr('Folder "{p0}" created.', p0=f"{folder.name}"), CONFIRMATION_MESSAGE_TIMEOUT_MS
    )


def _rename_connection_folder(self: MainWindow, folder_id: object) -> None:
    """Rename one canonical folder without changing its descendants."""
    if self._close_waiting_for_terminals or not isinstance(folder_id, UUID):
        return
    folder = self._connection_catalog.folder_by_id(folder_id)
    if folder is None:
        self.statusBar().showMessage(tr("Could not find the folder to rename."))
        return
    parent_folder_id = self._connection_catalog.parent_id_of(folder.folder_id)
    dialog = ConnectionFolderDialog(
        self, folder=folder, reserved_names=self._sibling_connection_folder_names(parent_folder_id)
    )
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        renamed_folder = dialog.connection_folder
    finally:
        schedule_delete_later(dialog)
    if renamed_folder.folder_id != folder.folder_id or renamed_folder.child_ids != folder.child_ids:
        self.statusBar().showMessage(tr("Renamed folder returned unexpected contents."))
        return
    try:
        candidate = self._connection_catalog.with_folder_renamed(
            folder.folder_id, renamed_folder.name
        )
    except ValueError as error:
        self.statusBar().showMessage(tr("Could not rename folder: {p0}", p0=f"{error}"))
        return
    if not self._commit_connection_catalog(candidate, select_item_id=folder.folder_id):
        return
    self.statusBar().showMessage(
        tr('Folder "{p0}" renamed.', p0=f"{renamed_folder.name}"), CONFIRMATION_MESSAGE_TIMEOUT_MS
    )


def _delete_connection_folder(self: MainWindow, folder_id: object) -> None:
    """Delete an explicitly confirmed empty folder only."""
    if self._close_waiting_for_terminals or not isinstance(folder_id, UUID):
        return
    folder = self._connection_catalog.folder_by_id(folder_id)
    if folder is None:
        self.statusBar().showMessage(tr("Could not find the folder to delete."))
        return
    if folder.child_ids:
        self.statusBar().showMessage(
            tr("This folder is not empty. Move or delete its contents first.")
        )
        return
    if not self._confirm_connection_folder_deletion(folder):
        return
    parent_folder_id = self._connection_catalog.parent_id_of(folder.folder_id)
    try:
        candidate = self._connection_catalog.without_empty_folder(folder.folder_id)
    except ValueError as error:
        self.statusBar().showMessage(tr("Could not delete folder: {p0}", p0=f"{error}"))
        return
    if not self._commit_connection_catalog(candidate, select_item_id=parent_folder_id):
        return
    self.statusBar().showMessage(
        tr('Folder "{p0}" deleted.', p0=f"{folder.name}"), CONFIRMATION_MESSAGE_TIMEOUT_MS
    )


def _move_connection_catalog_item(
    self: MainWindow, item_id: object, parent_folder_id: object, index: object
) -> None:
    """Persist one sidebar move before reprojecting the catalog tree."""
    if self._close_waiting_for_terminals:
        return
    if not isinstance(item_id, UUID):
        self.statusBar().showMessage(tr("Could not move connection item: invalid item ID."))
        return
    if parent_folder_id is not None and (not isinstance(parent_folder_id, UUID)):
        self.statusBar().showMessage(
            tr("Could not move connection item: invalid destination folder ID.")
        )
        return
    if index is not None and type(index) is not int:
        self.statusBar().showMessage(
            tr("Could not move connection item: invalid destination position.")
        )
        return
    item = self._connection_catalog.folder_by_id(item_id)
    if item is None:
        item = self._connection_catalog.profile_by_id(item_id)
    if item is None:
        self.statusBar().showMessage(tr("Could not find the connection item to move."))
        return
    destination_folder = None
    if parent_folder_id is not None:
        destination_folder = self._connection_catalog.folder_by_id(parent_folder_id)
        if destination_folder is None:
            self.statusBar().showMessage(tr("Could not find the destination connection folder."))
            return
    current_parent_id = self._connection_catalog.parent_id_of(item_id)
    try:
        candidate = self._connection_catalog.with_item_moved(
            item_id, parent_folder_id=parent_folder_id, index=index
        )
    except ValueError as error:
        self.statusBar().showMessage(tr("Could not move connection item: {p0}", p0=f"{error}"))
        return
    if candidate is self._connection_catalog:
        return
    if not self._commit_connection_catalog(candidate, select_item_id=item_id):
        return
    item_kind = tr("Folder") if isinstance(item, ConnectionFolder) else tr("Connection")
    destination = (
        tr("User sessions")
        if destination_folder is None
        else tr('folder "{p0}"', p0=f"{destination_folder.name}")
    )
    action = tr("reordered in") if current_parent_id == parent_folder_id else tr("moved to")
    self.statusBar().showMessage(
        f'{item_kind} "{item.name}" {action} {destination}.', CONFIRMATION_MESSAGE_TIMEOUT_MS
    )


def _resolve_connection_folder_id(self: MainWindow, value: object) -> tuple[bool, UUID | None]:
    """Resolve one root/folder destination received from the passive sidebar."""
    if value is None:
        return (True, None)
    if isinstance(value, UUID) and self._connection_catalog.folder_by_id(value):
        return (True, value)
    self.statusBar().showMessage(tr("The selected connection folder no longer exists."))
    return (False, None)


def _sibling_connection_folder_names(
    self: MainWindow, parent_folder_id: UUID | None
) -> tuple[str, ...]:
    """Return sibling labels for inline duplicate validation in the dialog."""
    return tuple(

            item.name
            for item in self._connection_catalog.children_of(parent_folder_id)
            if isinstance(item, ConnectionFolder)

    )


def _confirm_connection_folder_deletion(self: MainWindow, folder: ConnectionFolder) -> bool:
    """Ask before deleting an empty organizational folder."""
    message_box = QMessageBox(self)
    message_box.setObjectName("deleteConnectionFolderDialog")
    message_box.setWindowTitle(tr("Delete folder"))
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setTextFormat(Qt.TextFormat.PlainText)
    message_box.setText(tr('Delete the empty folder "{p0}"?', p0=f"{folder.name}"))
    message_box.setInformativeText(
        tr(
            "Only the folder will be removed. Saved connections are never deleted through this action."
        )
    )
    delete_button = QPushButton(tr("Delete folder"), message_box)
    delete_button.setObjectName("deleteConnectionFolderButton")
    message_box.addButton(delete_button, QMessageBox.ButtonRole.DestructiveRole)
    cancel_button = QPushButton(tr("Cancel"), message_box)
    cancel_button.setObjectName("cancelConnectionFolderButton")
    message_box.addButton(cancel_button, QMessageBox.ButtonRole.RejectRole)
    message_box.setDefaultButton(cancel_button)
    message_box.setEscapeButton(cancel_button)
    try:
        message_box.exec()
        return message_box.clickedButton() is delete_button
    finally:
        schedule_delete_later(message_box)


def _confirm_connection_deletion(self: MainWindow, profile: ConnectionProfile) -> bool:
    """Ask the user before permanently deleting a saved profile."""
    message_box = QMessageBox(self)
    message_box.setObjectName("deleteConnectionDialog")
    message_box.setWindowTitle(tr("Delete connection"))
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setTextFormat(Qt.TextFormat.PlainText)
    message_box.setText(tr('Delete "{p0}"?', p0=f"{profile.name}"))
    message_box.setInformativeText(
        tr("This removes the saved profile. This action cannot be undone.")
    )
    delete_button = QPushButton(tr("Delete"), message_box)
    delete_button.setObjectName("deleteConnectionButton")
    message_box.addButton(delete_button, QMessageBox.ButtonRole.DestructiveRole)
    cancel_button = QPushButton(tr("Cancel"), message_box)
    cancel_button.setObjectName("cancelDeleteButton")
    message_box.addButton(cancel_button, QMessageBox.ButtonRole.RejectRole)
    message_box.setDefaultButton(cancel_button)
    message_box.setEscapeButton(cancel_button)
    try:
        message_box.exec()
        return message_box.clickedButton() is delete_button
    finally:
        schedule_delete_later(message_box)
