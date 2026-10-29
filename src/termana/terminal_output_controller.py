"""User-authorized output export, isolated from terminal input and transport."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QDialog, QFileDialog, QWidget
from shiboken6 import isValid

from termana.credentials.background_operation import BackgroundOperation
from termana.dialogs.action_dialog import ActionDialog
from termana.i18n import tr
from termana.qt_lifecycle import schedule_delete_later
from termana.storage.terminal_output import (
    TerminalOutputError,
    prepare_terminal_output_target,
    write_terminal_output,
)
from termana.widgets.terminal_workspace import TerminalWorkspace


class TerminalOutputController(QObject):
    """Own at most one file operation and keep it alive through window shutdown."""

    message = Signal(str)
    finished = Signal()

    def __init__(
        self,
        window: QWidget,
        workspace: TerminalWorkspace,
        *,
        operation_starter: Callable[[BackgroundOperation], None] | None = None,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._workspace = workspace
        self._start = operation_starter or QThreadPool.globalInstance().start
        self._operation: BackgroundOperation | None = None
        self._operation_id: UUID | None = None
        self._choosing = False
        self._closing = False
        self._confirmation: ActionDialog | None = None
        self._file_dialog: QFileDialog | None = None

    @property
    def is_busy(self) -> bool:
        return self._operation is not None

    def stop_accepting_exports(self) -> None:
        """Cancel unconfirmed dialogs while letting an authorized write finish."""
        self._closing = True
        for dialog in (self._confirmation, self._file_dialog):
            if dialog is not None and isValid(dialog):
                # Accepted window shutdown disables the parent next. End owned
                # modal loops first so no disabled dialog can keep Qt alive.
                dialog.reject()

    def _source_available(self, session_id: UUID) -> bool:
        return not self._closing and self._workspace.terminal_output_export_available(session_id)

    @Slot(object)
    def request_export(self, session_id: object) -> None:
        """Capture before dialogs, then recheck ownership before dispatching any write."""
        if not isinstance(session_id, UUID) or not self._source_available(session_id):
            return
        if self.is_busy or self._choosing:
            self.message.emit(tr("A terminal output export is already in progress."))
            return
        snapshot = self._workspace.capture_terminal_output(session_id)
        if snapshot is None:
            return
        self._choosing = True
        try:
            if not self._confirm_export() or not self._source_available(session_id):
                return
            filename = self._choose_path()
            if not filename or not self._source_available(session_id):
                return
            try:
                target = prepare_terminal_output_target(Path(filename))
            except (TerminalOutputError, OSError, ValueError):
                self.message.emit(
                    tr("Cannot save output to that file. Choose a regular writable file.")
                )
                return
            operation_id = uuid4()
            task = BackgroundOperation(
                operation_id, lambda: write_terminal_output(target, snapshot)
            )
            self._operation_id = operation_id
            self._operation = task
            task.signals.succeeded.connect(self._succeeded)
            task.signals.failed.connect(self._failed)
            self.message.emit(tr("Saving terminal output…"))
            try:
                self._start(task)
            except Exception:
                # A synchronous starter may already have delivered its completion.
                self._failed(operation_id)
        finally:
            self._choosing = False

    def _confirm_export(self) -> bool:
        dialog = ActionDialog(
            object_name="saveTerminalOutputDialog",
            window_title=tr("Save terminal output"),
            title=tr("Save terminal output?"),
            subtitle=tr("A snapshot, not an ongoing session recording"),
            body=(
                tr(
                    "Save the current screen and retained history as UTF-8 text (up to 16 MiB). Full-screen applications export only their current screen.\n\nServer output can contain sensitive data. Review the file before sharing it."
                )
            ),
            primary_text=tr("Choose file…"),
            primary_object_name="chooseOutputFileButton",
            secondary_text=tr("Cancel"),
            secondary_object_name="cancelOutputExportButton",
            badge_text="TXT",
            parent=self._window,
        )
        self._confirmation = dialog
        try:
            return not self._closing and dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._confirmation = None
            if isValid(dialog):
                schedule_delete_later(dialog)

    def _choose_path(self) -> str:
        """Own the save chooser so accepted shutdown can end its nested event loop."""
        dialog = QFileDialog(self._window, tr("Save terminal output"))
        dialog.setObjectName("saveTerminalOutputFileDialog")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter(tr("Text files (*.txt)"))
        dialog.selectFile("terminal-output.txt")
        self._file_dialog = dialog
        try:
            if self._closing or dialog.exec() != QDialog.DialogCode.Accepted:
                return ""
            filenames = dialog.selectedFiles()
            return filenames[0] if filenames else ""
        finally:
            self._file_dialog = None
            if isValid(dialog):
                schedule_delete_later(dialog)

    def _take_operation(self, operation_id: object) -> bool:
        if self._operation is None or operation_id != self._operation_id:
            return False
        self._operation = None
        self._operation_id = None
        return True

    @Slot(object, object)
    def _succeeded(self, operation_id: object, _byte_count: object) -> None:
        if self._take_operation(operation_id):
            self.message.emit(tr("Terminal output saved."))
            self.finished.emit()

    @Slot(object)
    def _failed(self, operation_id: object) -> None:
        if self._take_operation(operation_id):
            self.message.emit(
                tr(
                    "Could not save output. Check file permissions and free space; the output limit is 16 MiB. Try again with a different filename."
                )
            )
            self.finished.emit()
