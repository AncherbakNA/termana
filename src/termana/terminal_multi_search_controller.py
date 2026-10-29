"""One explicit, cancellable frozen-output search, with GUI-only owner tokens."""

from collections.abc import Callable
from threading import Event
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from termana.credentials.background_operation import ConfidentialBackgroundOperation
from termana.dialogs.terminal_multi_search_dialog import TerminalMultiSearchDialog
from termana.i18n import tr
from termana.models.terminal_multi_search import (
    MAX_MULTI_SEARCH_QUERY,
    MAX_MULTI_SEARCH_SESSIONS,
    TerminalMultiSearchResult,
    TerminalSearchCapture,
    TerminalSearchHit,
)
from termana.qt_lifecycle import schedule_delete_later
from termana.terminal.multi_search import search_terminal_snapshots
from termana.widgets.terminal_workspace import TerminalWorkspace


class TerminalMultiSearchController(QObject):
    message = Signal(str)

    def __init__(
        self,
        window: QWidget,
        workspace: TerminalWorkspace,
        *,
        operation_starter: Callable | None = None,
    ):
        super().__init__(window)
        self._window, self._workspace = window, workspace
        self._start = operation_starter or QThreadPool.globalInstance().start
        self._dialog = None
        self._operation = None
        self._operation_id = None
        self._cancel = Event()
        self._capture = TerminalSearchCapture()
        self._epoch = 0
        self._operation_epoch = 0
        self._operation_parameters = None
        self._closing = False

    def open_dialog(self, _checked: bool = False) -> None:
        if self._closing or not self._workspace.accepts_new_terminals:
            return
        if self._dialog is not None and isValid(self._dialog):
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        self._epoch += 1
        dialog = TerminalMultiSearchDialog(
            self._window, sessions=self._workspace.open_terminal_sessions()
        )
        self._dialog = dialog
        dialog.search_requested.connect(self._search)
        dialog.refresh_requested.connect(self._refresh)
        dialog.result_requested.connect(self._request_switch)
        dialog.finished.connect(lambda _: self._finished(dialog))
        dialog.set_busy(self._operation is not None)
        dialog.show()

    def _active(self) -> bool:
        return not self._closing and self._dialog is not None and isValid(self._dialog)

    def _refresh(self) -> None:
        if self._active() and self._operation is None:
            self._dialog.set_sessions(self._workspace.open_terminal_sessions())

    def _search(self) -> None:
        if not self._active() or self._operation is not None:
            return
        dialog = self._dialog
        ids, query = dialog.selected_session_ids, dialog.query_input.text()
        if (
            not query
            or len(query) > MAX_MULTI_SEARCH_QUERY
            or not 1 <= len(ids) <= MAX_MULTI_SEARCH_SESSIONS
        ):
            dialog.show_status(tr("Enter 1-512 literal characters and check 1-16 terminals."))
            return
        self._capture = self._workspace.capture_terminal_search_sources(ids)
        dialog.clear_results()
        if not self._capture.sources:
            dialog.set_result(
                TerminalMultiSearchResult(),
                sources=0,
                skipped=self._capture.skipped,
                budget_skipped=self._capture.budget_skipped,
            )
            return
        # Qt backend identity tokens remain here on the GUI thread. Only immutable
        # plain metadata/snapshots enter the worker and confidential result mailbox.
        documents = tuple(source.document for source in self._capture.sources)
        case_sensitive, whole_words = (
            dialog.case_sensitive.isChecked(),
            dialog.whole_words.isChecked(),
        )
        self._cancel = Event()
        cancelled = self._cancel.is_set
        operation_id = uuid4()
        task = ConfidentialBackgroundOperation(
            operation_id,
            lambda: search_terminal_snapshots(
                documents,
                query,
                case_sensitive=case_sensitive,
                whole_words=whole_words,
                cancelled=cancelled,
            ),
        )
        self._operation_id, self._operation = operation_id, task
        self._operation_epoch = self._epoch
        self._operation_parameters = (ids, query, case_sensitive, whole_words)
        task.signals.succeeded.connect(self._succeeded)
        task.signals.failed.connect(self._failed)
        dialog.set_busy(True)
        try:
            self._start(task)
        except Exception:
            self._failed(operation_id)

    def _succeeded(self, operation_id: UUID) -> None:
        if operation_id != self._operation_id or self._operation is None:
            return
        result = self._operation.take_result(operation_id)
        self._complete(operation_id, result)

    def _failed(self, operation_id: UUID) -> None:
        self._complete(operation_id, None)

    def _complete(self, operation_id: UUID, result: object) -> None:
        if operation_id != self._operation_id:
            return
        current = self._operation_epoch == self._epoch
        parameters, self._operation_parameters = self._operation_parameters, None
        self._operation = self._operation_id = None
        if not self._active():
            self._capture = TerminalSearchCapture()
            return
        self._dialog.set_busy(False)
        if not current:
            return
        dialog = self._dialog
        if parameters != (
            dialog.selected_session_ids,
            dialog.query_input.text(),
            dialog.case_sensitive.isChecked(),
            dialog.whole_words.isChecked(),
        ):
            dialog.show_status(tr("Search options changed. Search again; no terminal was changed."))
            self._capture = TerminalSearchCapture()
            return
        if type(result) is not TerminalMultiSearchResult or result.cancelled:
            self._dialog.show_status(
                tr("Search cancelled or unavailable. No terminal was changed.")
            )
            self._capture = TerminalSearchCapture()
            return
        self._dialog.set_result(
            result,
            sources=len(self._capture.sources),
            skipped=self._capture.skipped,
            budget_skipped=self._capture.budget_skipped,
        )

    def _owner_matches(self, session_id: UUID, generation: object) -> bool:
        return (
            self._workspace.terminal_session_generation(session_id) is generation
            and self._workspace.terminal_output_export_available(session_id)
            and not self._workspace.terminal_reconnect_pending(session_id)
        )

    def _request_switch(self, hit: object) -> None:
        if (
            not self._active()
            or self._operation is not None
            or type(hit) is not TerminalSearchHit
            or self._dialog.selected_hit is not hit
        ):
            return
        source = next(
            (
                source
                for source in self._capture.sources
                if source.document.session.session_id == hit.session_id
            ),
            None,
        )
        if source is None or not self._owner_matches(hit.session_id, source.generation):
            self._dialog.show_status(
                tr(
                    "That terminal owner changed or is unavailable. Search again; nothing was selected."
                )
            )
            return
        dialog, generation = self._dialog, source.generation
        foreground = dialog.isActiveWindow()
        dialog.accept()
        epoch = self._epoch
        if foreground and self._window.isVisible():
            self._window.activateWindow()
        QTimer.singleShot(0, self, lambda: self._activate(epoch, hit.session_id, generation))

    def _activate(self, epoch: int, session_id: UUID, generation: object) -> None:
        if self._closing or epoch != self._epoch or self._dialog is not None:
            return
        if not self._owner_matches(
            session_id, generation
        ) or not self._workspace.activate_terminal_session(session_id):
            self.message.emit(tr("That terminal changed before it could be selected."))

    def _finished(self, dialog: TerminalMultiSearchDialog) -> None:
        if self._dialog is dialog:
            self._dialog = None
            self._epoch += 1
            self._cancel.set()
            self._capture = TerminalSearchCapture()
        schedule_delete_later(dialog)

    def stop(self) -> None:
        self._closing = True
        self._epoch += 1
        self._cancel.set()
        if self._dialog is not None and isValid(self._dialog):
            self._dialog.reject()
