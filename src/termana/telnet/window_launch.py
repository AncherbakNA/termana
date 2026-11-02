"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from uuid import UUID

from termana.app.window_contracts import (
    CONFIRMATION_MESSAGE_TIMEOUT_MS,
    LOGGER,
    PLACEHOLDER_MESSAGE_TIMEOUT_MS,
)
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.storage.telnet_warning_store import TelnetWarningStoreError
from termana.telnet.dialogs.telnet_warning_dialog import confirm_telnet_connection
from termana.ui.i18n import tr


def _open_telnet_connection(
    self: MainWindow,
    profile: ConnectionProfile,
    *,
    reconnect_session_id: UUID | None = None,
    pane_id: UUID | None = None,
) -> bool:
    """Confirm and open one unencrypted Telnet profile through its own backend."""
    if self._close_waiting_for_terminals or (
        reconnect_session_id is not None
        and (
            not isinstance(reconnect_session_id, UUID)
            or not self.terminal_workspace.terminal_reconnect_pending(reconnect_session_id)
        )
    ):
        return False
    if profile.protocol is not ConnectionProtocol.TELNET:
        self.statusBar().showMessage(tr("This connection protocol is not supported yet."))
        return False
    if pane_id is None:
        pane_id = (
            self.terminal_workspace.pane_id_for_session(reconnect_session_id)
            if reconnect_session_id is not None
            else self.terminal_workspace.current_pane_id
        )
    if pane_id is None or not self.terminal_workspace.pane_available(pane_id):
        return False
    if not self._confirm_telnet_connection(profile):
        if not self._close_waiting_for_terminals:
            self.statusBar().showMessage(
                tr("Telnet connection canceled."), CONFIRMATION_MESSAGE_TIMEOUT_MS
            )
        return False
    if (
        self._close_waiting_for_terminals
        or not self.terminal_workspace.pane_available(pane_id)
        or (
            reconnect_session_id is not None
            and (not self.terminal_workspace.terminal_reconnect_pending(reconnect_session_id))
        )
    ):
        return False
    try:
        backend = self._telnet_backend_factory(profile.host, profile.port, profile.telnet_encoding)
    except ValueError as error:
        LOGGER.warning("Could not prepare Telnet backend: %s", error)
        self.statusBar().showMessage(
            tr("Could not start Telnet because the connection settings are invalid."),
            PLACEHOLDER_MESSAGE_TIMEOUT_MS,
        )
        return False
    terminal_session_id = self.terminal_workspace.open_backend_terminal_session(
        profile.name,
        backend,
        requires_direct_input=True,
        reconnect_profile=profile,
        pane_id=pane_id,
    )
    if terminal_session_id is None:
        self.statusBar().showMessage(
            tr("Interactive Telnet is not available right now."), PLACEHOLDER_MESSAGE_TIMEOUT_MS
        )
        return False
    self.statusBar().showMessage(
        tr('Telnet terminal "{p0}" opened.', p0=f"{profile.name}"), CONFIRMATION_MESSAGE_TIMEOUT_MS
    )
    return True


def _confirm_telnet_connection(self: MainWindow, profile: ConnectionProfile) -> bool:
    """Require fresh consent unless this Telnet endpoint was explicitly remembered."""
    return confirm_telnet_connection(
        host=profile.host, port=profile.port, parent=self, consent_store=self._telnet_warning_store
    )


def _reset_telnet_warnings(self: MainWindow) -> None:
    try:
        self._telnet_warning_store.clear()
    except TelnetWarningStoreError:
        self.statusBar().showMessage(tr("Could not reset Telnet warning choices."), 8000)
    else:
        self.statusBar().showMessage(tr("Telnet warnings will be shown again."), 5000)
