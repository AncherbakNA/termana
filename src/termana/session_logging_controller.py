"""Exact-session, explicit recording consent; default is always off."""
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QDialog
from shiboken6 import isValid

from termana.dialogs.action_dialog import ActionDialog
from termana.i18n import tr
from termana.paths import session_logs_directory
from termana.qt_lifecycle import schedule_delete_later
from termana.widgets.terminal_page import TerminalPage


class SessionLoggingController(QObject):
    def __init__(self, window, workspace):
        super().__init__(window)
        self._window, self._workspace = window, workspace
        self._watched = set()
        self.action = QAction(tr('Start session logging…'), window)
        self.action.setObjectName('sessionLoggingAction')
        self.action.triggered.connect(self.toggle)
        self.open_action = QAction(tr('Open session logs folder…'), window)
        self.open_action.setObjectName('openSessionLogsAction')
        self.open_action.triggered.connect(self.open_folder)
        workspace.active_terminal_changed.connect(self.refresh)
        workspace.open_sessions_changed.connect(self.refresh)
        self.refresh()

    def page(self):
        page = self._workspace.tabs.currentWidget()
        return page if isinstance(page, TerminalPage) else None

    def refresh(self, *_):
        page = self.page()
        if page is not None and page.session_id not in self._watched:
            self._watched.add(page.session_id)
            identity = page.session_id
            page.destroyed.connect(lambda *_: self._watched.discard(identity))
            page.logging_changed.connect(self.refresh)
            page.process_started.connect(self.refresh)
            page.process_finished.connect(self.refresh)
        self.action.setText(tr('Stop session logging') if page and page.logging_active else tr('Start session logging…'))
        self.action.setEnabled(bool(page and (page.logging_active or page.logging_available)))
        self.open_action.setEnabled(session_logs_directory().is_dir())

    def toggle(self, *_):
        page = self.page()
        if page is None:
            return
        if page.logging_active:
            page.stop_session_logging()
            self.refresh()
            return
        if not page.logging_available:
            return
        dialog = ActionDialog(
            object_name='startSessionLoggingDialog', window_title=tr('Record session output'),
            title=tr('Start session logging?'), subtitle=tr('Only this session · Off by default'),
            body=tr('Record future server/shell output as UTF-8 in ~/.termana/logs. Output may contain secrets; review files before sharing. Keyboard input and authentication responses are not recorded. Logging stops before a new authentication prompt.\n\nFiles rotate at 5 MiB, up to 3 parts per recording; then recording stops. The folder is capped at 64 MiB. Existing logs are not deleted automatically.'),
            primary_text=tr('Start logging'), primary_object_name='startSessionLoggingButton',
            secondary_text=tr('Cancel'), secondary_object_name='cancelSessionLoggingButton',
            badge_text='LOG', parent=self._window,
        )
        try:
            if (dialog.exec() == QDialog.DialogCode.Accepted and isValid(page)
                    and self.page() is page and page.logging_available):
                page.start_session_logging(session_logs_directory())
        finally:
            schedule_delete_later(dialog)
            self.refresh()

    def open_folder(self, *_):
        path = session_logs_directory()
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
