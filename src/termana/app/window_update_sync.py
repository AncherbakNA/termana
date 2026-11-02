"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from termana.storage.connection_store import ConnectionStoreError


def _show_update_dialog(self: MainWindow, _checked=False):
    dialog = _update_dialog(self)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    if dialog.stage_job is None:
        dialog.check()


def _update_dialog(self: MainWindow, *, directory=None):
    from termana.updates.ui import UpdateDialog

    dialog = getattr(self, "_update_dialog", None)
    if dialog is None:
        dialog = self._update_dialog = UpdateDialog(
            self, directory=directory, installer=self._request_update_install
        )
    return dialog


def start_automatic_updates(self: MainWindow, directory):
    """Share the manual dialog/worker, never create a second checkpoint writer."""
    dialog = _update_dialog(self, directory=directory)
    if dialog._automatic_started:
        return

    def available(version):
        if self._close_waiting_for_terminals:
            return
        self.check_updates_action.setText(f"Update available: {version}…")
        self.statusBar().showMessage(
            f"Termana {version} is available. Open Help → Check for updates to download and install.",
            20_000,
        )

    dialog.automatic_update_available.connect(available)
    dialog.automatic_check_failed.connect(
        lambda: self.statusBar().showMessage(
            "Automatic update check could not finish. Use Help → Check for updates to retry.",
            15_000,
        ) if not self._close_waiting_for_terminals else None
    )
    dialog.start_automatic_checks()


def _request_update_install(self: MainWindow, job):
    self._update_install_job = job
    self.close()
    if not self._application_close_confirmed:
        self._update_install_job = None


def _show_sync_dialog(self: MainWindow, _checked=False):
    from termana.profile_sync.ui import SyncDialog

    if (
        not self._connection_store_ready
        or self._connection_store is None
        or self._close_waiting_for_terminals
    ):
        self.statusBar().showMessage("Saved connections must be loaded before synchronization.")
        return
    dialog = getattr(self, "_sync_dialog", None)
    if dialog is None:
        dialog = self._sync_dialog = SyncDialog(
            self,
            store=self._connection_store,
            catalog=lambda: self._connection_catalog,
            commit=self._commit_sync_catalog,
            disconnect=self._disconnect_sync_catalog,
            reload_catalog=self._reload_sync_catalog,
        )
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def _commit_sync_catalog(self: MainWindow, plan, state):
    if (
        self._close_waiting_for_terminals
        or self._connection_transfer_operation is not None
        or (not self._connection_store_ready)
    ):
        raise ValueError("Another catalog operation is active")
    plan.check_current(self._connection_catalog, plan.source_identities)
    self._connection_store.save_sync(plan, state)
    if self._connection_catalog != plan.catalog:
        self._apply_connection_catalog(plan.catalog)


def _disconnect_sync_catalog(self: MainWindow):
    if (
        self._close_waiting_for_terminals
        or self._connection_transfer_operation is not None
        or (not self._connection_store_ready)
    ):
        raise ValueError("Another catalog operation is active")
    self._connection_store.disconnect_sync()


def _reload_sync_catalog(self: MainWindow):
    if self._close_waiting_for_terminals or self._connection_transfer_operation is not None:
        raise ValueError("Another catalog operation is active")
    try:
        catalog = self._connection_store.load()
    except ConnectionStoreError:
        self._connection_store_ready = False
        self.connections_sidebar.set_catalog_available(False)
        self._update_connection_transfer_actions()
        raise
    self._connection_store_ready = True
    self._apply_connection_catalog(catalog)
    self.connections_sidebar.set_catalog_available(True)
