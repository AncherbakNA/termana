"""Terminal navigation and reviewed ended-tab cleanup with live UUID checks."""

from uuid import UUID

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QDialog, QWidget
from shiboken6 import isValid

from termana.dialogs.close_ended_terminals_dialog import CloseEndedTerminalsDialog
from termana.dialogs.open_terminals_dialog import OpenTerminalsDialog
from termana.i18n import tr
from termana.models.open_terminal import OpenTerminalState
from termana.qt_lifecycle import schedule_delete_later
from termana.widgets.terminal_workspace import TerminalWorkspace

OPEN_TERMINALS_REFRESH_MS = 100


class OpenTerminalsController(QObject):
    """Never interpret a remembered list row as a different live terminal."""

    message = Signal(str)

    def __init__(self, window: QWidget, workspace: TerminalWorkspace) -> None:
        super().__init__(window)
        self._window = window
        self._workspace = workspace
        self._dialog: OpenTerminalsDialog | None = None
        self._close_review: CloseEndedTerminalsDialog | None = None
        self._closing = False
        self._generation = 0
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(OPEN_TERMINALS_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh)
        workspace.open_sessions_changed.connect(self._queue_refresh)

    def open_dialog(self, _checked: bool = False) -> None:
        if self._closing or not self._workspace.accepts_new_terminals:
            return
        if self._dialog is not None and isValid(self._dialog):
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        # A new picker supersedes any deferred activation from an earlier one.
        self._generation += 1
        dialog = OpenTerminalsDialog(
            self._window, sessions=self._workspace.open_terminal_sessions()
        )
        self._dialog = dialog
        dialog.session_requested.connect(self._request_session)
        dialog.close_ended_requested.connect(self._request_close_ended)
        dialog.finished.connect(lambda _result: self._finished(dialog))
        dialog.open()

    def _queue_refresh(self) -> None:
        # No polling, no snapshot work while closed, and continuous renaming
        # cannot postpone updates indefinitely by restarting the timer.
        if self._active() and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _active(self) -> bool:
        return not self._closing and self._dialog is not None and isValid(self._dialog)

    def _refresh(self) -> None:
        if self._active():
            self._dialog.set_sessions(self._workspace.open_terminal_sessions())

    def _request_session(self, session_id: object) -> None:
        if not self._active() or self._close_review is not None or not isinstance(session_id, UUID):
            return
        dialog = self._dialog
        if (
            dialog.selected_session_id != session_id
            or not self._workspace.contains_terminal_session(session_id)
            or not self._workspace.accepts_new_terminals
        ):
            dialog.set_sessions(self._workspace.open_terminal_sessions())
            dialog.show_error(tr("That terminal is no longer available. Select an open terminal."))
            return
        self._generation += 1
        generation = self._generation
        was_foreground = dialog.isActiveWindow()
        # Qt first restores the dialog's previous focus. Select the intended
        # terminal on the next tick, after that restoration, and recheck its UUID.
        dialog.accept()
        if was_foreground and not self._closing and self._window.isVisible():
            self._window.activateWindow()
        QTimer.singleShot(0, self, lambda: self._activate(generation, session_id))

    def _request_close_ended(self, session_ids: object) -> None:
        if (
            not self._active()
            or self._close_review is not None
            or not self._workspace.accepts_new_terminals
            or type(session_ids) is not tuple
            or any(type(item) is not UUID for item in session_ids)
        ):
            return
        dialog = self._dialog
        displayed = dialog.visible_ended_sessions
        if session_ids != tuple(session.session_id for session in displayed):
            return
        dialog.show_result("")
        dialog.show_error("")
        # Refresh can lag behind a backend signal. Intersect, never broaden the
        # list the user actually saw with terminals that have just ended.
        live = {session.session_id: session for session in self._workspace.open_terminal_sessions()}
        reviewed = tuple(
            live[session_id]
            for session_id in session_ids
            if session_id in live and live[session_id].state is OpenTerminalState.ENDED
        )
        if not reviewed:
            self._refresh()
            dialog.show_error(tr("No matching ended terminals remain. Nothing was closed."))
            return
        review = CloseEndedTerminalsDialog(dialog, sessions=reviewed)
        self._close_review = review
        review.finished.connect(lambda result: self._close_review_finished(review, dialog, result))
        review.open()

    def _close_review_finished(
        self, review: CloseEndedTerminalsDialog, owner: OpenTerminalsDialog, result: int
    ) -> None:
        current = self._close_review is review
        if current:
            self._close_review = None
        if (
            current
            and self._active()
            and self._dialog is owner
            and result == QDialog.DialogCode.Accepted
        ):
            closed = self._workspace.close_ended_terminal_sessions(review.session_ids)
            self._refresh()
            skipped = len(review.session_ids) - len(closed)
            message = tr(
                "Closed {p0} ended terminals; retained output was discarded.", p0=f"{len(closed)}"
            )
            if not closed:
                message = tr("No terminals were closed.")
            if skipped:
                message += tr(" Skipped {p0} no longer eligible terminals.", p0=f"{skipped}")
            if self._active() and self._dialog is owner:
                owner.show_result(message)
            self.message.emit(message)
        schedule_delete_later(review)

    def _activate(self, generation: int, session_id: UUID) -> None:
        if self._closing or generation != self._generation or self._dialog is not None:
            return
        if not self._workspace.activate_terminal_session(session_id):
            self.message.emit(tr("That terminal closed before it could be selected."))

    def _finished(self, dialog: OpenTerminalsDialog) -> None:
        if self._dialog is dialog:
            self._dialog = None
            self._refresh_timer.stop()
            if self._close_review is not None and isValid(self._close_review):
                self._close_review.reject()
        schedule_delete_later(dialog)

    def stop(self) -> None:
        """Cancel pending navigation before the window begins terminal teardown."""
        self._closing = True
        self._generation += 1
        self._refresh_timer.stop()
        if self._dialog is not None and isValid(self._dialog):
            self._dialog.reject()
