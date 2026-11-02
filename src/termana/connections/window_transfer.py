"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFileDialog

from termana.app.background_operation import BackgroundOperation
from termana.app.qt_lifecycle import schedule_delete_later
from termana.app.window_contracts import (
    CONFIRMATION_MESSAGE_TIMEOUT_MS,
    CONNECTION_TRANSFER_FILE_FILTER,
    DEFAULT_CONNECTION_EXPORT_NAME,
    _ConnectionTransferKind,
    _ConnectionTransferOperationContext,
    _PendingConnectionImportCommit,
)
from termana.connections.dialogs import connection_transfer_dialogs as transfer_dialogs_module
from termana.connections.dialogs.connection_transfer_dialogs import (
    confirm_connection_export,
    confirm_connection_import,
)
from termana.models.connection import ConnectionCatalog, ConnectionFolder, ConnectionProtocol
from termana.models.connection_export import catalog_for_connection_export
from termana.storage.connection_store import ConnectionStore
from termana.storage.connection_transfer import (
    ConnectionTransferError,
    ConnectionTransferStats,
    DecodedConnectionTransfer,
    decode_connection_transfer,
    encode_connection_catalog,
    merge_connection_transfer,
)
from termana.storage.connection_transfer_io import (
    ConnectionTransferPathError,
    PreparedConnectionExportTarget,
    prepare_connection_export_target,
    read_connection_transfer_bytes,
    write_prepared_connection_transfer_bytes,
)
from termana.ui.i18n import count_noun, tr


def _open_connection_import_dialog(self: MainWindow, _checked: bool = False) -> None:
    """Select and parse one portable catalog without blocking the interface."""
    if (
        self._close_waiting_for_terminals
        or not self._connection_store_ready
        or self._connection_transfer_operation is not None
        or self._connection_transfer_confirmation_active
    ):
        return
    self._connection_transfer_confirmation_active = True
    self._update_connection_transfer_actions()
    try:
        selected_path = self._choose_connection_transfer_path(
            importing=True, title=tr("Import connections"), default_path=""
        )
    finally:
        self._connection_transfer_confirmation_active = False
        self._update_connection_transfer_actions()
    if not selected_path or self._close_waiting_for_terminals:
        return
    source_path = Path(selected_path)

    def import_connections(path: Path = source_path) -> object:
        payload = read_connection_transfer_bytes(path)
        return decode_connection_transfer(payload)

    self._start_connection_transfer_operation(
        kind=_ConnectionTransferKind.IMPORT, path=source_path, operation=import_connections
    )


def _open_connection_export_dialog(self: MainWindow, _checked: bool = False) -> None:
    """Export the entire catalog snapshot captured before the file picker."""
    self._export_connection_catalog(
        self._connection_catalog,
        scope_label=tr("All saved connections"),
        dialog_title=tr("Export all connections"),
    )


def _open_connection_item_export_dialog(self: MainWindow, item_id: object) -> None:
    """Resolve one exact saved item; a stale target never falls back to all."""
    if not isinstance(item_id, UUID) or not self._connection_export_available():
        return
    catalog = self._connection_catalog
    item = catalog.folder_by_id(item_id) or catalog.profile_by_id(item_id)
    if item is None:
        self.statusBar().showMessage(tr("Could not find the connection or folder to export."))
        return
    is_folder = isinstance(item, ConnectionFolder)
    self._export_connection_catalog(
        catalog_for_connection_export(catalog, item_id),
        scope_label=f"{('Folder' if is_folder else 'Connection')} · {item.name}",
        dialog_title=tr("Export folder") if is_folder else tr("Export connection"),
        include_folder_contents=is_folder,
    )


def _connection_export_available(self: MainWindow) -> bool:
    """Guard every export entry point, including requests from stale menus."""
    return bool(
        not self._close_waiting_for_terminals
        and self._connection_store_ready
        and (self._connection_transfer_operation is None)
        and (not self._connection_transfer_confirmation_active)
    )


def _export_connection_catalog(
    self: MainWindow,
    catalog: ConnectionCatalog,
    *,
    scope_label: str,
    dialog_title: str,
    include_folder_contents: bool = False,
) -> None:
    """Confirm and export exactly the reviewed snapshot, never later additions."""
    if not self._connection_export_available() or not catalog.root_item_ids:
        return
    stats = ConnectionTransferStats(
        ssh_connections=sum(
            profile.protocol is ConnectionProtocol.SSH for profile in catalog.profiles
        ),
        telnet_connections=sum(
            profile.protocol is ConnectionProtocol.TELNET for profile in catalog.profiles
        ),
        folders=len(catalog.folders),
        rdp_connections=sum(
            profile.protocol is ConnectionProtocol.RDP for profile in catalog.profiles
        ),
    )
    self._connection_transfer_confirmation_active = True
    self._update_connection_transfer_actions()
    try:
        selected_path = self._choose_connection_transfer_path(
            importing=False, title=dialog_title, default_path=DEFAULT_CONNECTION_EXPORT_NAME
        )
        if not selected_path or self._close_waiting_for_terminals:
            return
        connections_path = self._active_connection_store_path()
        try:
            prepared_target = prepare_connection_export_target(
                Path(selected_path), connections_path
            )
        except ConnectionTransferPathError:
            self.statusBar().showMessage(
                tr(
                    "Choose a regular file in an existing writable folder. Termana's working connections file cannot be used."
                )
            )
            return
        destination_path = prepared_target.path
        try:
            export_confirmed = self._confirm_connection_transfer(
                confirm_connection_export,
                destination_path=destination_path,
                stats=stats,
                scope_label=scope_label,
                include_folder_contents=include_folder_contents,
                parent=self,
            )
        except Exception:
            self._show_connection_transfer_failure(_ConnectionTransferKind.EXPORT)
            return
        if self._close_waiting_for_terminals:
            return
        if not export_confirmed:
            self.statusBar().showMessage(
                tr("Connection export canceled."), CONFIRMATION_MESSAGE_TIMEOUT_MS
            )
            return
    finally:
        self._connection_transfer_confirmation_active = False
        self._update_connection_transfer_actions()
    if not self._connection_export_available():
        return

    def export_connections(
        snapshot: ConnectionCatalog = catalog,
        target: PreparedConnectionExportTarget = prepared_target,
        exported_stats: ConnectionTransferStats = stats,
    ) -> object:
        payload = encode_connection_catalog(snapshot)
        write_prepared_connection_transfer_bytes(target, payload)
        return exported_stats

    self._start_connection_transfer_operation(
        kind=_ConnectionTransferKind.EXPORT, path=destination_path, operation=export_connections
    )


def _active_connection_store_path(self: MainWindow) -> Path | None:
    """Return the real working data path when the configured store exposes one."""
    path = getattr(self._connection_store, "path", None)
    return path if isinstance(path, Path) else None


def _register_connection_transfer_dialog(self: MainWindow, dialog: QDialog) -> bool:
    """Bind one modal decision to this live window before its nested loop."""
    if self._close_waiting_for_terminals or self._connection_transfer_dialog is not None:
        return False
    self._connection_transfer_dialog = dialog
    return True


def _choose_connection_transfer_path(
    self: MainWindow, *, importing: bool, title: str, default_path: str
) -> str:
    """Own the native picker so accepted shutdown can reject it safely."""
    dialog = QFileDialog(self, title)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setObjectName(
        "importConnectionsFileDialog" if importing else "exportConnectionsFileDialog"
    )
    dialog.setAcceptMode(
        QFileDialog.AcceptMode.AcceptOpen if importing else QFileDialog.AcceptMode.AcceptSave
    )
    dialog.setFileMode(
        QFileDialog.FileMode.ExistingFile if importing else QFileDialog.FileMode.AnyFile
    )
    dialog.setNameFilter(tr(CONNECTION_TRANSFER_FILE_FILTER))
    if default_path:
        dialog.selectFile(default_path)
    try:
        if not self._register_connection_transfer_dialog(dialog):
            return ""
        if dialog.exec() != QDialog.DialogCode.Accepted or self._close_waiting_for_terminals:
            return ""
        paths = dialog.selectedFiles()
        return paths[0] if paths else ""
    finally:
        if self._connection_transfer_dialog is dialog:
            self._connection_transfer_dialog = None
        schedule_delete_later(dialog)


def _confirm_connection_transfer(
    self: MainWindow, provider: Callable[..., bool], **values: object
) -> bool:
    """Register built-in decisions without changing injected provider contracts."""
    if self._close_waiting_for_terminals:
        return False
    if provider in (
        transfer_dialogs_module.confirm_connection_import,
        transfer_dialogs_module.confirm_connection_export,
    ):
        values["dialog_created"] = self._register_connection_transfer_dialog
    previous_dialog = self._connection_transfer_dialog
    try:
        return provider(**values)
    finally:
        self._connection_transfer_dialog = previous_dialog


def _start_connection_transfer_operation(
    self: MainWindow,
    *,
    kind: _ConnectionTransferKind,
    path: Path,
    operation: Callable[[], object],
    pending_import_commit: _PendingConnectionImportCommit | None = None,
) -> bool:
    """Start the only active transfer and retain it across queued completion."""
    if not isinstance(kind, _ConnectionTransferKind):
        raise TypeError("kind must be a connection transfer kind")
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    if not callable(operation):
        raise TypeError("operation must be callable")
    if pending_import_commit is not None and (
        not isinstance(pending_import_commit, _PendingConnectionImportCommit)
    ):
        raise TypeError("pending_import_commit must be an import commit snapshot")
    if (kind is _ConnectionTransferKind.IMPORT_COMMIT) != (pending_import_commit is not None):
        raise ValueError("pending_import_commit is required only for an import commit")
    if (
        self._close_waiting_for_terminals
        or self._connection_transfer_operation is not None
        or self._connection_transfer_confirmation_active
    ):
        return False
    operation_id = uuid4()
    task = BackgroundOperation(operation_id, operation)
    context = _ConnectionTransferOperationContext(
        operation_id=operation_id,
        kind=kind,
        path=path,
        task=task,
        pending_import_commit=pending_import_commit,
    )
    task.signals.succeeded.connect(self._handle_connection_transfer_succeeded)
    task.signals.failed.connect(self._handle_connection_transfer_failed)
    self._connection_transfer_operation = context
    self._connection_transfer_progress.setAccessibleName(
        tr("Importing connections")
        if kind is _ConnectionTransferKind.IMPORT
        else tr("Saving imported connections")
        if kind is _ConnectionTransferKind.IMPORT_COMMIT
        else tr("Exporting connections")
    )
    self._connection_transfer_progress.show()
    self.statusBar().showMessage(
        tr("Reading and checking the connection file…")
        if kind is _ConnectionTransferKind.IMPORT
        else tr("Saving imported connections…")
        if kind is _ConnectionTransferKind.IMPORT_COMMIT
        else tr("Writing the connection export…")
    )
    if kind is _ConnectionTransferKind.IMPORT_COMMIT:
        self.new_connection_action.setEnabled(False)
        self.new_folder_action.setEnabled(False)
        self.connections_sidebar.setEnabled(False)
    self._update_connection_transfer_actions()
    try:
        self._connection_transfer_operation_starter(task)
    except Exception:
        if self._connection_transfer_operation is not context:
            return True
        self._finish_connection_transfer_operation(operation_id)
        self._show_connection_transfer_failure(kind)
        return False
    return True


def _handle_connection_transfer_succeeded(
    self: MainWindow, operation_id: object, result: object
) -> None:
    """Consume one exact transfer result and ignore duplicates or stale tasks."""
    context = self._finish_connection_transfer_operation(operation_id)
    if context is None:
        return
    if self._close_waiting_for_terminals:
        self.close()
        return
    if context.kind is _ConnectionTransferKind.IMPORT_COMMIT:
        pending = context.pending_import_commit
        if result is not True or pending is None:
            self._show_connection_transfer_failure(context.kind)
            return
        self._apply_connection_catalog(pending.catalog, select_item_id=pending.select_item_id)
        self.statusBar().showMessage(
            self._connection_transfer_completion_message("Imported", pending.stats),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )
        return
    if context.kind is _ConnectionTransferKind.EXPORT:
        if not isinstance(result, ConnectionTransferStats):
            self._show_connection_transfer_failure(context.kind)
            return
        self.statusBar().showMessage(
            self._connection_transfer_completion_message("Exported", result),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )
        return
    if not isinstance(result, DecodedConnectionTransfer):
        self._show_connection_transfer_failure(context.kind)
        return
    self._confirm_and_commit_connection_import(context.path, result)


def _handle_connection_transfer_failed(self: MainWindow, operation_id: object) -> None:
    """Reduce file, codec, and unexpected worker failures to a safe UI message."""
    context = self._finish_connection_transfer_operation(operation_id)
    if context is None:
        return
    if self._close_waiting_for_terminals:
        self.close()
        return
    self._show_connection_transfer_failure(context.kind)


def _finish_connection_transfer_operation(
    self: MainWindow, operation_id: object
) -> _ConnectionTransferOperationContext | None:
    """Release only the exact active operation and restore action availability."""
    context = self._connection_transfer_operation
    if (
        context is None
        or not isinstance(operation_id, UUID)
        or operation_id != context.operation_id
    ):
        return None
    self._connection_transfer_operation = None
    self._connection_transfer_progress.hide()
    if context.kind is _ConnectionTransferKind.IMPORT_COMMIT:
        self.connections_sidebar.setEnabled(True)
        self.new_connection_action.setEnabled(self._connection_store_ready)
        self.new_folder_action.setEnabled(self._connection_store_ready)
    self._update_connection_transfer_actions()
    return context


def _confirm_and_commit_connection_import(
    self: MainWindow, source_path: Path, decoded: DecodedConnectionTransfer
) -> None:
    """Merge against the latest catalog, preview exact counts, then persist once."""
    try:
        preview = merge_connection_transfer(self._connection_catalog, decoded)
    except ConnectionTransferError:
        self._show_connection_transfer_failure(_ConnectionTransferKind.IMPORT)
        return
    if preview.stats.total_items == 0:
        self.statusBar().showMessage(
            tr("The selected connection file is valid but contains no items."),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )
        return
    self._connection_transfer_confirmation_active = True
    self._update_connection_transfer_actions()
    import_confirmation_failed = False
    try:
        import_confirmed = self._confirm_connection_transfer(
            confirm_connection_import, source_path=source_path, stats=preview.stats, parent=self
        )
    except Exception:
        import_confirmed = False
        import_confirmation_failed = True
    finally:
        self._connection_transfer_confirmation_active = False
        self._update_connection_transfer_actions()
    if import_confirmation_failed:
        self._show_connection_transfer_failure(_ConnectionTransferKind.IMPORT)
        return
    if not import_confirmed:
        self.statusBar().showMessage(
            tr("Connection import canceled."), CONFIRMATION_MESSAGE_TIMEOUT_MS
        )
        return
    if self._close_waiting_for_terminals:
        return
    base_catalog = self._connection_catalog
    try:
        merged = merge_connection_transfer(base_catalog, decoded)
    except ConnectionTransferError:
        self._show_connection_transfer_failure(_ConnectionTransferKind.IMPORT)
        return
    first_imported_root_index = len(base_catalog.root_item_ids)
    select_item_id = merged.catalog.root_item_ids[first_imported_root_index]
    pending_commit = _PendingConnectionImportCommit(
        catalog=merged.catalog, stats=merged.stats, select_item_id=select_item_id
    )
    store = self._connection_store
    if store is None:
        self._apply_connection_catalog(
            pending_commit.catalog, select_item_id=pending_commit.select_item_id
        )
        self.statusBar().showMessage(
            self._connection_transfer_completion_message("Imported", pending_commit.stats),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )
        return
    if not self._connection_store_ready:
        self.statusBar().showMessage(
            tr("Saving is disabled because the connection file could not be loaded.")
        )
        return

    def persist_import(
        selected_store: ConnectionStore = store,
        candidate: ConnectionCatalog = pending_commit.catalog,
    ) -> object:
        selected_store.save(candidate)
        return True

    self._start_connection_transfer_operation(
        kind=_ConnectionTransferKind.IMPORT_COMMIT,
        path=source_path,
        operation=persist_import,
        pending_import_commit=pending_commit,
    )


def _show_connection_transfer_failure(self: MainWindow, kind: _ConnectionTransferKind) -> None:
    """Show actionable, non-sensitive failure text for one transfer direction."""
    if kind is _ConnectionTransferKind.IMPORT:
        message = tr(
            "Could not import connections. The selected file is unreadable, invalid, unsupported, or too large."
        )
    elif kind is _ConnectionTransferKind.IMPORT_COMMIT:
        message = tr(
            "Could not save the imported connections. Existing connections remain unchanged."
        )
    else:
        message = tr("Could not export connections. The selected destination was not written.")
    self.statusBar().showMessage(message)


def _connection_transfer_completion_message(verb: str, stats: ConnectionTransferStats) -> str:
    """Return one compact status summary without exposing endpoint metadata."""
    if not isinstance(verb, str) or not verb:
        raise ValueError("verb must be non-empty text")
    if not isinstance(stats, ConnectionTransferStats):
        raise TypeError("stats must be ConnectionTransferStats")
    connection_noun = count_noun("connection", "connections", stats.connections)
    folder_noun = count_noun("folder", "folders", stats.folders)
    return tr(
        "{p0} {p1} {p2} and {p3} {p4}.",
        p0=tr(verb, context="connection-transfer"),
        p1=f"{stats.connections}",
        p2=f"{connection_noun}",
        p3=f"{stats.folders}",
        p4=f"{folder_noun}",
    )


def _update_connection_transfer_actions(self: MainWindow) -> None:
    """Keep menu availability aligned with catalog safety and active work."""
    available = bool(
        self._connection_store_ready
        and (not self._close_waiting_for_terminals)
        and (self._connection_transfer_operation is None)
        and (not self._connection_transfer_confirmation_active)
    )
    self.import_connections_action.setEnabled(available)
    self.export_connections_action.setEnabled(
        available and bool(self._connection_catalog.root_item_ids)
    )
    self.save_ssh_session_action.setEnabled(
        self.terminal_workspace.current_ssh_session_id is not None
        and self._ssh_session_save_available()
    )
