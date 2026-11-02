"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

from uuid import UUID

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from termana.app.qt_lifecycle import schedule_delete_later
from termana.app.window_contracts import (
    HOST_KEY_ERROR_MESSAGE_CHARACTERS,
    _SshHostKeyRecovery,
    _SshLoginResolution,
)
from termana.models.connection import ConnectionProfile
from termana.models.ssh_jump import SshJumpMode
from termana.ssh.dialogs.ssh_host_key_dialog import SshHostKeyDialog
from termana.ssh.known_hosts import SshHostKeyMismatch, known_hosts_target
from termana.ssh.ssh_tunnel_diagnostics import SshChannelFailure, channel_failure_guidance
from termana.ui.i18n import tr


def _show_ssh_authentication_status(self: MainWindow, message: str) -> None:
    """Render a bounded non-secret status emitted by an auth session."""
    if self._close_waiting_for_terminals or not isinstance(message, str):
        return
    self.statusBar().showMessage(message[:HOST_KEY_ERROR_MESSAGE_CHARACTERS])


def _show_ssh_channel_failure(self: MainWindow, authentication_id: UUID, kind: object) -> None:
    """Route fixed advisory text to the owner, never whichever tab is focused."""
    if (
        self._close_waiting_for_terminals
        or type(kind) is not SshChannelFailure
        or authentication_id not in self._ssh_authentication_sessions
    ):
        return
    session_id = self._ssh_authentication_terminal_ids.get(authentication_id)
    if session_id is not None:
        self.terminal_workspace.show_connection_guidance(session_id, channel_failure_guidance(kind))


def _handle_ssh_secret_requested(
    self: MainWindow, authentication_id: UUID, request: object
) -> None:
    """Forward the existing auth signal to the exact-owner prompt router."""
    self._ssh_secret_prompts.request(authentication_id, request)


def _handle_ssh_secret_request_finished(
    self: MainWindow, authentication_id: UUID, request: object
) -> None:
    """Forward completion without resolving the currently selected tab."""
    self._ssh_secret_prompts.finish(authentication_id, request)


def _handle_inline_ssh_secret_submitted(
    self: MainWindow, terminal_session_id: object, request_id: object, secret: str
) -> None:
    """Deliver a terminal-owned secret only to its exact live auth session."""
    try:
        self._ssh_secret_prompts.submit(terminal_session_id, request_id, secret)
    finally:
        secret = ""


def _handle_inline_ssh_secret_cancel_requested(
    self: MainWindow, terminal_session_id: object, request_id: object
) -> None:
    """Cancel only the AskPass request shown in the exact terminal tab."""
    self._ssh_secret_prompts.cancel(terminal_session_id, request_id)


def _cancel_ssh_authentication(self: MainWindow, authentication_id: UUID) -> None:
    """Close the owning terminal after the user cancels a direct AskPass prompt."""
    if authentication_id not in self._ssh_authentication_sessions:
        self._pending_ssh_authentication_cancellations.discard(authentication_id)
        return
    terminal_session_id = self._ssh_authentication_terminal_ids.get(authentication_id)
    if terminal_session_id is None:
        self._pending_ssh_authentication_cancellations.add(authentication_id)
        return
    self.terminal_workspace.close_terminal_session(terminal_session_id)


def _close_ssh_authentication(
    self: MainWindow, authentication_id: UUID, *, collect_diagnostics: bool = True
) -> bytes:
    """Forget one flow and return any private OpenSSH diagnostics it retained."""
    authentication = self._ssh_authentication_sessions.pop(authentication_id, None)
    self._ssh_authentication_terminal_ids.pop(authentication_id, None)
    self._pending_ssh_authentication_cancellations.discard(authentication_id)
    if authentication is None:
        return b""
    try:
        if collect_diagnostics:
            return authentication.finish()
        authentication.close()
        return b""
    finally:
        authentication.setParent(None)
        self._ssh_secret_prompts.clear_authentication(authentication_id)


def _confirm_ssh_host_key_replacement(
    self: MainWindow, profile: ConnectionProfile, mismatch: SshHostKeyMismatch
) -> bool:
    """Warn clearly before removing an obsolete local trust record."""
    endpoint = known_hosts_target(profile.host, profile.port)
    dialog = SshHostKeyDialog(
        endpoint=endpoint,
        received_key_type=mismatch.received_key_type or "UNKNOWN",
        received_fingerprint=mismatch.received_fingerprint or "Unavailable",
        stored_key_type=mismatch.stored_key_type,
        known_hosts_path=mismatch.known_hosts_path,
        line_number=mismatch.line_number,
        parent=self,
    )
    return self._exec_ssh_host_key_dialog(dialog) == QDialog.DialogCode.Accepted


def _exec_ssh_host_key_dialog(self: MainWindow, dialog: QDialog) -> int:
    """Keep each nested host-key decision owned until its modal loop exits."""
    self._ssh_host_key_dialogs.add(dialog)
    try:
        if self._close_waiting_for_terminals:
            return QDialog.DialogCode.Rejected
        return dialog.exec()
    finally:
        self._ssh_host_key_dialogs.discard(dialog)
        schedule_delete_later(dialog)


def _show_manual_host_key_warning(
    self: MainWindow, profile: ConnectionProfile, mismatch: SshHostKeyMismatch
) -> None:
    """Explain why ambiguous host identity or trust files require inspection."""
    message_box = QMessageBox(self)
    message_box.setObjectName("manualSshHostKeyDialog")
    message_box.setWindowTitle(tr("Server identity changed"))
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setTextFormat(Qt.TextFormat.PlainText)
    endpoint = known_hosts_target(profile.host, profile.port)
    route_details = ""
    if profile.ssh_jump.mode is SshJumpMode.JUMP:
        message_box.setText(tr("OpenSSH reported a changed host key on this connection route."))
        message_box.setInformativeText(
            tr(
                "This connection uses a jump host. The warning may concern the gateway or the destination, so no saved keys will be changed here. Inspect the terminal output and verify the reported server and fingerprint through a trusted channel before reconnecting."
            )
        )
        route_details = tr(
            "Configured jump host: {p0}\n", p0=f"{profile.ssh_jump.proxy_jump_value}"
        )
    else:
        message_box.setText(tr('The SSH host key for "{p0}" has changed.', p0=f"{endpoint}"))
        message_box.setInformativeText(
            tr(
                "Termana only performs automatic recovery when OpenSSH reports this exact endpoint in the standard user known_hosts file. SSH aliases, canonicalized hosts, and custom trust files require manual inspection. Verify the presented fingerprint through a trusted channel first."
            )
        )
    message_box.setDetailedText(
        tr(
            "{p0}Presented fingerprint: {p1}\nExpected target: {p2}\nReported target: {p3}\nReported file: {p4}\nOffending line: {p5}",
            p0=f"{route_details}",
            p1=f"{mismatch.received_fingerprint or 'Not reported by OpenSSH.'}",
            p2=f"{endpoint}",
            p3=f"{mismatch.host_key_subject}",
            p4=f"{mismatch.known_hosts_path}",
            p5=f"{mismatch.line_number}",
        )
    )
    self._exec_ssh_host_key_dialog(message_box)


def _show_unverifiable_host_key_warning(
    self: MainWindow, profile: ConnectionProfile, mismatch: SshHostKeyMismatch
) -> None:
    """Refuse automatic recovery when no fingerprint can be verified."""
    endpoint = known_hosts_target(profile.host, profile.port)
    message_box = QMessageBox(self)
    message_box.setObjectName("unverifiableSshHostKeyDialog")
    message_box.setWindowTitle(tr("Server identity changed"))
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setTextFormat(Qt.TextFormat.PlainText)
    message_box.setText(tr('The SSH host key for "{p0}" has changed.', p0=f"{endpoint}"))
    message_box.setInformativeText(
        tr(
            "OpenSSH did not provide a fingerprint that Termana could display. No saved key will be removed automatically. Inspect the terminal output and verify the server identity through a trusted channel."
        )
    )
    message_box.setDetailedText(
        tr(
            "known_hosts file: {p0}\nOffending line: {p1}",
            p0=f"{mismatch.known_hosts_path}",
            p1=f"{mismatch.line_number}",
        )
    )
    self._exec_ssh_host_key_dialog(message_box)


def _handle_known_hosts_removal_finished(
    self: MainWindow, context: object, succeeded: bool, message: str
) -> None:
    """Retry normal OpenSSH verification only after a guarded removal."""
    if self._close_waiting_for_terminals:
        self.close()
        return
    if not isinstance(context, _SshHostKeyRecovery):
        return
    if not succeeded:
        concise_message = message[:HOST_KEY_ERROR_MESSAGE_CHARACTERS]
        self.statusBar().showMessage(
            tr("Could not complete SSH host-key recovery: {p0}", p0=f"{concise_message}")
        )
        self.terminal_workspace.show_connection_guidance(
            context.failed_session_id,
            tr("Could not complete SSH host-key recovery: {p0}", p0=f"{concise_message}"),
        )
        self.terminal_workspace.finish_terminal_reconnect(
            context.failed_session_id, succeeded=False
        )
        return
    pane_id = self.terminal_workspace.pane_id_for_session(context.failed_session_id)
    if pane_id is None:
        self.statusBar().showMessage(
            tr(
                "The saved SSH host-key entry is no longer present, but the closed terminal was not reopened."
            )
        )
        return
    if not self._start_resolved_ssh_connection(
        _SshLoginResolution(
            profile=context.profile,
            credential_id=context.credential_id,
            credential_generation=context.credential_generation,
        ),
        recovery_attempted=True,
        pane_id=pane_id,
        source_session_id=context.failed_session_id,
        host_key_approval=context.approval,
        launch_guard=lambda: self.terminal_workspace.contains_terminal_session(
            context.failed_session_id
        ),
    ):
        self.terminal_workspace.finish_terminal_reconnect(
            context.failed_session_id, succeeded=False
        )
        self.terminal_workspace.show_connection_guidance(
            context.failed_session_id,
            tr(
                "The old key entry was removed, but SSH could not continue safely. The tab and history were kept. Verify the server identity before trying again."
            ),
        )
        return
    self.statusBar().showMessage(
        tr("Continuing SSH in the same tab with verification of the reviewed fingerprint.")
    )
