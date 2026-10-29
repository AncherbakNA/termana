"""Keep a read-only details window bound to an exact launched SSH session."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialog, QFileDialog, QWidget
from shiboken6 import isValid

from termana.dialogs.ssh_session_details_dialog import SshSessionDetailsDialog
from termana.i18n import tr
from termana.models.connection import ConnectionProfile
from termana.models.ssh_negotiation import SshNegotiatedFacts
from termana.models.ssh_session_status import SshSessionStatus
from termana.qt_lifecycle import schedule_delete_later
from termana.ssh_session_report import ssh_session_report_bytes
from termana.terminal.private_text_export import export_private_text_copy
from termana.widgets.terminal_workspace import TerminalWorkspace


@dataclass(frozen=True, slots=True)
class _ReportCapture:
    session_id: UUID
    generation: object
    profile: ConnectionProfile
    status: SshSessionStatus | None
    negotiation: SshNegotiatedFacts | None
    revision: int


class SshSessionDetailsController(QObject):
    def __init__(self, window: QWidget, workspace: TerminalWorkspace) -> None:
        super().__init__(window)
        self._window = window
        self._workspace = workspace
        self._dialog: SshSessionDetailsDialog | None = None
        self._session_id: UUID | None = None
        self._profile: ConnectionProfile | None = None
        self._generation: object | None = None
        self._file_dialog: QFileDialog | None = None
        self._choosing = False
        self._closing = False
        workspace.ssh_session_details_requested.connect(self.open_session)
        workspace.open_sessions_changed.connect(self._check_owner)

    def open_dialog(self, _checked: bool = False) -> None:
        self.open_session(self._workspace.current_ssh_session_id)

    def open_session(self, session_id: object) -> None:
        if self._closing or not isinstance(session_id, UUID):
            return
        profile = self._workspace.ssh_session_profile(session_id)
        generation = self._workspace.terminal_session_generation(session_id)
        if profile is None or generation is None:
            return
        if self._dialog is not None and isValid(self._dialog):
            if (
                self._session_id == session_id
                and self._generation is generation
                and self._profile is profile
            ):
                self._dialog.raise_()
                self._dialog.activateWindow()
                return
            self._dialog.reject()
        dialog = SshSessionDetailsDialog(
            self._window,
            profile=profile,
            status=self._workspace.ssh_session_status(session_id),
            negotiation=self._workspace.ssh_session_negotiation(session_id),
        )
        self._dialog = dialog
        self._session_id = session_id
        self._profile, self._generation = profile, generation
        dialog.finished.connect(lambda _result: self._finished(dialog))
        dialog.export_report_requested.connect(lambda: self._export_report(dialog))
        dialog.show()

    def _check_owner(self) -> None:
        # No timer, output subscription or metadata scan while the window is closed.
        if self._dialog is not None and isValid(self._dialog):
            if (
                self._workspace.ssh_session_profile(self._session_id) is not self._profile
                or self._workspace.terminal_session_generation(self._session_id)
                is not self._generation
            ):
                self._reject_export_picker()
                self._dialog.reject()
            elif (status := self._workspace.ssh_session_status(self._session_id)) is not None:
                self._dialog.set_session_state(
                    status, self._workspace.ssh_session_negotiation(self._session_id)
                )

    def _finished(self, dialog: SshSessionDetailsDialog) -> None:
        if self._dialog is dialog:
            self._reject_export_picker()
            self._dialog = None
            self._session_id = None
            self._profile = self._generation = None
        schedule_delete_later(dialog)

    def _report_current(self, dialog: SshSessionDetailsDialog, capture: _ReportCapture) -> bool:
        """No active-tab lookup or backend inspection: compare the exact opaque owner."""
        return (
            not self._closing
            and isValid(dialog)
            and dialog.isVisible()
            and self._dialog is dialog
            and self._session_id == capture.session_id
            and self._workspace.ssh_session_profile(capture.session_id) is capture.profile
            and self._workspace.terminal_session_generation(capture.session_id)
            is capture.generation
            and self._workspace.ssh_session_status(capture.session_id) == capture.status
            and self._workspace.ssh_session_negotiation(capture.session_id) == capture.negotiation
            and dialog.report_revision == capture.revision
        )

    def _export_report(self, dialog: SshSessionDetailsDialog) -> None:
        if (
            self._closing
            or self._choosing
            or self._dialog is not dialog
            or not isValid(dialog)
            or not dialog.isVisible()
            or self._session_id is None
        ):
            return
        self._choosing = True
        dialog.set_export_busy(True)
        dialog.set_export_feedback("")
        try:
            dialog.redact_checkbox.setChecked(True)
            self._check_owner()
            if self._dialog is not dialog or not isValid(dialog) or not dialog.isVisible():
                return
            status = self._workspace.ssh_session_status(self._session_id)
            facts = self._workspace.ssh_session_negotiation(self._session_id)
            dialog.set_session_state(status, facts)
            capture = _ReportCapture(
                self._session_id,
                self._generation,
                self._profile,
                status,
                facts,
                dialog.report_revision,
            )
            try:
                data = ssh_session_report_bytes(capture.profile, status=status, negotiation=facts)
            except (TypeError, ValueError):
                dialog.set_export_feedback(tr("This SSH report could not be prepared safely."))
                return
            filename = self._choose_export_path(dialog)
            if not filename:
                return
            if not self._report_current(dialog, capture):
                if self._dialog is dialog and isValid(dialog):
                    dialog.set_export_feedback(
                        tr("The SSH report changed. Choose Save redacted again.")
                    )
                return
            try:
                export_private_text_copy(Path(filename), data)
            except (OSError, ValueError):
                dialog.set_export_feedback(
                    tr(
                        "Report export could not be confirmed. Choose a new filename in a supported private local folder. Existing files are never replaced."
                    )
                )
                return
            dialog.set_export_feedback(tr("Redacted report saved. No SSH request was sent."))
        finally:
            self._choosing = False
            if isValid(dialog):
                dialog.set_export_busy(False)

    def _choose_export_path(self, dialog: SshSessionDetailsDialog) -> str:
        picker = QFileDialog(dialog, tr("Save redacted SSH report"))
        picker.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        picker.setFileMode(QFileDialog.FileMode.AnyFile)
        picker.setDefaultSuffix("txt")
        picker.setNameFilter(tr("Text reports (*.txt)"))
        picker.selectFile("ssh-session-report.txt")
        # A native overwrite question is not permission to replace a raced target.
        picker.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        self._file_dialog = picker
        try:
            accepted = picker.exec() == QDialog.DialogCode.Accepted
            paths = picker.selectedFiles() if accepted and isValid(picker) else []
            return paths[0] if len(paths) == 1 else ""
        except RuntimeError:
            return ""  # Parent destruction can retire the native picker while exec unwinds.
        finally:
            if self._file_dialog is picker:
                self._file_dialog = None
            if isValid(picker):
                schedule_delete_later(picker)

    def _reject_export_picker(self) -> None:
        if self._file_dialog is not None and isValid(self._file_dialog):
            self._file_dialog.reject()

    def stop(self) -> None:
        self._closing = True
        self._reject_export_picker()
        if self._dialog is not None and isValid(self._dialog):
            self._dialog.reject()
