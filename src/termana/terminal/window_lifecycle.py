"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from uuid import UUID

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog
from shiboken6 import isValid

from termana.app.window_contracts import (
    CONFIRMATION_MESSAGE_TIMEOUT_MS,
    LOGGER,
    _ConnectionOpenState,
    _SshConnectOrigin,
    _SshHostKeyRecovery,
    _SshSessionContext,
)
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.models.ssh_credential import SshEndpoint
from termana.models.ssh_jump import SshJumpMode
from termana.ssh.host_key_approval import SshHostKeyApproval
from termana.ssh.known_hosts import (
    has_ssh_host_key_warning,
    known_hosts_target,
    parse_ssh_host_key_mismatch,
)
from termana.ssh.ssh_connection_diagnostics import (
    ssh_connection_failure_guidance,
    ssh_keepalive_timeout_guidance,
)
from termana.ssh.ssh_tunnel_diagnostics import local_forward_failure_guidance
from termana.ui.i18n import tr


def _open_local_terminal(self: MainWindow, _checked: bool = False) -> None:
    """Ask the terminal workspace to create a local session tab."""
    if self._close_waiting_for_terminals:
        return
    self.terminal_workspace.open_local_terminal()


def _open_private_recovery_dialog(self: MainWindow, _checked: bool = False) -> None:
    """Open recovery only on explicit request, with one modeless owner per window."""
    if self._close_waiting_for_terminals:
        return
    dialog = self._private_recovery_dialog
    if dialog is None or not isValid(dialog):
        from termana.terminal.dialogs.private_recovery_dialog import PrivateRecoveryDialog

        dialog = PrivateRecoveryDialog(self)
        self._private_recovery_dialog = dialog
        dialog.finished.connect(lambda _result: self._release_private_recovery_dialog(dialog))
        dialog.destroyed.connect(lambda: self._release_private_recovery_dialog(dialog))
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def _release_private_recovery_dialog(self: MainWindow, dialog: QDialog) -> None:
    """A retired dialog cannot clear a newly opened recovery owner."""
    if self._private_recovery_dialog is dialog:
        self._private_recovery_dialog = None


def _show_local_terminal_opened(self: MainWindow, terminal_name: str) -> None:
    """Report creation of a local shell tab."""
    self.statusBar().showMessage(
        tr('Local terminal "{p0}" opened.', p0=f"{terminal_name}"), CONFIRMATION_MESSAGE_TIMEOUT_MS
    )


def _show_terminal_closed(self: MainWindow, terminal_name: str) -> None:
    """Report closure of a terminal tab."""
    self.statusBar().showMessage(
        tr('Terminal "{p0}" closed.', p0=f"{terminal_name}"), CONFIRMATION_MESSAGE_TIMEOUT_MS
    )


def _handle_connection_reconnect_requested(
    self: MainWindow, session_id: object, profile: object
) -> None:
    """Route an ended tab through the ordinary protocol and authentication flow."""
    if not isinstance(session_id, UUID):
        return
    if (
        self._close_waiting_for_terminals
        or not isinstance(profile, ConnectionProfile)
        or (not self.terminal_workspace.terminal_reconnect_pending(session_id))
    ):
        self.terminal_workspace.finish_terminal_reconnect(session_id, succeeded=False)
        return
    if profile.protocol is ConnectionProtocol.SSH:
        outcome = self._open_ssh_connection(
            profile, origin=_SshConnectOrigin.RECONNECT, reconnect_session_id=session_id
        )
    elif profile.protocol is ConnectionProtocol.TELNET:
        outcome = (
            _ConnectionOpenState.ACCEPTED
            if self._open_telnet_connection(profile, reconnect_session_id=session_id)
            else _ConnectionOpenState.REJECTED
        )
    else:
        outcome = self._open_connection(profile)
    if outcome is _ConnectionOpenState.PENDING:
        return
    if self.terminal_workspace.terminal_reconnect_pending(session_id):
        self.terminal_workspace.finish_terminal_reconnect(session_id, succeeded=bool(outcome))


def _handle_command_terminal_finished(
    self: MainWindow, session_id: object, recent_output: bytes, context: object, exit_code: int
) -> None:
    """Explain SSH failures and offer guarded recovery for host-key conflicts."""
    diagnostics = b""
    approval_rejected = False
    if isinstance(context, _SshSessionContext) and context.authentication_id is not None:
        authentication = self._ssh_authentication_sessions.get(context.authentication_id)
        approval_rejected = bool(getattr(authentication, "host_key_approval_rejected", False))
        diagnostics = self._close_ssh_authentication(context.authentication_id)
        if isinstance(session_id, UUID) and diagnostics:
            self.terminal_workspace.append_terminal_diagnostic(session_id, diagnostics)
            recent_output = recent_output + b"\n" + diagnostics
    if approval_rejected and isinstance(session_id, UUID):
        self.terminal_workspace.show_connection_guidance(
            session_id,
            tr(
                "The presented SSH key could not be matched to your approval. No password was sent. Verify the server identity before reconnecting; no additional key was accepted."
            ),
        )
        return
    if (
        not self._close_waiting_for_terminals
        and isinstance(session_id, UUID)
        and isinstance(context, _SshSessionContext)
    ):
        guidance = local_forward_failure_guidance(
            recent_output,
            context.profile.ssh_tunnels,
            exit_code,
            via_jump=context.profile.ssh_jump.mode is not SshJumpMode.DIRECT,
        )
        if guidance is None:
            guidance = ssh_connection_failure_guidance(
                diagnostics,
                exit_code,
                via_jump=context.profile.ssh_jump.mode is not SshJumpMode.DIRECT,
            )
        if guidance is None:
            guidance = ssh_keepalive_timeout_guidance(
                recent_output,
                exit_code,
                via_jump=context.profile.ssh_jump.mode is not SshJumpMode.DIRECT,
            )
        if guidance is not None:
            self.terminal_workspace.show_connection_guidance(session_id, guidance)
    if (
        self._close_waiting_for_terminals
        or exit_code != 255
        or (not isinstance(session_id, UUID))
        or (not isinstance(context, _SshSessionContext))
        or (not self.terminal_workspace.contains_terminal_session(session_id))
    ):
        return
    mismatch = None if context.recovery_attempted else parse_ssh_host_key_mismatch(diagnostics)
    if mismatch is None:
        if has_ssh_host_key_warning(recent_output):
            key_status = (
                tr("No further key changes were made. ")
                if context.recovery_attempted
                else tr("No saved key was changed. ")
            )
            self.terminal_workspace.show_connection_guidance(
                session_id,
                tr("Automatic key recovery is unavailable for this host-key warning. ")
                + key_status
                + tr(
                    "Verify the server identity through a trusted channel and review your SSH configuration before reconnecting manually."
                ),
            )
        return
    if context.profile.ssh_jump.mode is SshJumpMode.JUMP:
        self._show_manual_host_key_warning(context.profile, mismatch)
        return
    if mismatch.received_fingerprint is None:
        self._show_unverifiable_host_key_warning(context.profile, mismatch)
        return
    expected_subject = known_hosts_target(context.profile.host, context.profile.port)
    if (
        mismatch.host_key_subject.casefold() != expected_subject.casefold()
        or not self._known_hosts_updater.manages_path(mismatch.known_hosts_path)
    ):
        self._show_manual_host_key_warning(context.profile, mismatch)
        return
    try:
        approval = SshHostKeyApproval(
            SshEndpoint(context.profile.host, context.profile.port),
            mismatch.received_key_type,
            mismatch.received_fingerprint,
        )
    except (TypeError, ValueError):
        self._show_manual_host_key_warning(context.profile, mismatch)
        return
    if not self._confirm_ssh_host_key_replacement(context.profile, mismatch):
        return
    if (
        self._close_waiting_for_terminals
        or not self.terminal_workspace.contains_terminal_session(session_id)
        or (not self.terminal_workspace.begin_host_key_recovery(session_id))
    ):
        return
    recovery = _SshHostKeyRecovery(
        failed_session_id=session_id,
        profile=context.profile,
        approval=approval,
        credential_id=context.credential_id,
        credential_generation=context.credential_generation,
    )
    self.statusBar().showMessage(tr("Checking the saved SSH host key..."))
    self.terminal_workspace.show_connection_guidance(
        session_id,
        tr("Updating the reviewed SSH host key. Your tab and terminal history stay open."),
    )
    self._known_hosts_updater.remove_host_key(
        recovery,
        host=context.profile.host,
        port=context.profile.port,
        line_number=mismatch.line_number,
        stored_key_type=mismatch.stored_key_type,
    )


def _handle_command_terminal_started(
    self: MainWindow, session_id: object, context: object, process_id: int
) -> None:
    """Bind AskPass to the exact process launched for this terminal tab."""
    if (
        not isinstance(session_id, UUID)
        or not isinstance(context, _SshSessionContext)
        or context.authentication_id is None
    ):
        return
    authentication = self._ssh_authentication_sessions.get(context.authentication_id)
    if authentication is None:
        return
    self._ssh_authentication_terminal_ids[context.authentication_id] = session_id
    try:
        authentication.bind_ssh_process(process_id)
    except (RuntimeError, TypeError, ValueError):
        LOGGER.warning("Could not bind secure SSH password handling to its process")
        self._close_ssh_authentication(context.authentication_id, collect_diagnostics=False)
        QTimer.singleShot(
            0,
            lambda session_id=session_id: self.terminal_workspace.close_terminal_session(
                session_id
            ),
        )


def _handle_command_terminal_closing(
    self: MainWindow, _session_id: object, context: object
) -> None:
    """Revoke AskPass before a manually closed terminal begins shutdown."""
    if not isinstance(context, _SshSessionContext) or context.authentication_id is None:
        return
    self._close_ssh_authentication(context.authentication_id, collect_diagnostics=False)
