"""Explicit config-to-effective-profile review; no silent inherited configuration."""

from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QThreadPool, Slot
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMessageBox
from shiboken6 import isValid

from termana.credentials.background_operation import BackgroundOperation
from termana.i18n import tr
from termana.terminal.ssh_config_snapshot import resolve_config_snapshot


class SshConfigController(QObject):
    def __init__(self, window, opener, available=lambda: True, operation_starter=None):
        super().__init__(window)
        self._window, self._opener = window, opener
        self._available = available
        self._starter = operation_starter or QThreadPool.globalInstance().start
        self._pending = None
        self._closed = False
        self._dialog = None
        self._choosing = False

    def _choose(self, dialog, value):
        self._dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            if (
                not accepted
                or not isValid(self)
                or not isValid(self._window)
                or not isValid(dialog)
                or self._closed
                or not self._available()
            ):
                return None
            return value(dialog)
        finally:
            self._dialog = None
            if isValid(dialog):
                dialog.deleteLater()

    def open(self, *_):
        if self._closed or not self._available() or self._pending is not None or self._choosing:
            return
        self._choosing = True
        try:
            picker = QFileDialog(self._window, tr("Choose supported SSH config"))
            picker.setFileMode(QFileDialog.FileMode.ExistingFile)
            filename = self._choose(picker, lambda value: next(iter(value.selectedFiles()), None))
            if not filename or self._closed:
                return
            prompt = QInputDialog(self._window)
            prompt.setWindowTitle(tr("SSH config host"))
            prompt.setLabelText(tr("Host alias from this file"))
            alias = self._choose(prompt, lambda value: value.textValue())
            if alias is None or self._closed:
                return
            identity = uuid4()
            task = BackgroundOperation(
                identity, lambda: resolve_config_snapshot(Path(filename).absolute(), alias)
            )
            self._pending = identity, task
            task.signals.succeeded.connect(self._resolved)
            task.signals.failed.connect(self._failed)
            self._window.statusBar().showMessage(
                tr("Reading the selected bounded SSH config; no connection yet…")
            )
            try:
                self._starter(task)
            except Exception:
                self._failed(identity)
        finally:
            self._choosing = False

    @Slot(object)
    def _failed(self, identity):
        if self._pending is None or self._pending[0] != identity:
            return
        self._pending = None
        if not self._closed:
            self._window.statusBar().showMessage(
                tr(
                    "Config refused or unavailable. Supported only: Host/HostName/Port/User, one explicit user@gateway, ConnectTimeout/ServerAlive. No Include, Match, commands, key/trust overrides or tokens. No connection was opened."
                )
            )

    @Slot(object, object)
    def _resolved(self, identity, profile):
        if self._pending is None or self._pending[0] != identity:
            return
        self._pending = None
        if self._closed or not self._available():
            return
        message = QMessageBox(self._window)
        message.setWindowTitle(tr("Review effective SSH connection"))
        message.setTextFormat(Qt.TextFormat.PlainText)
        route = profile.ssh_jump
        via = (
            "Direct"
            if route.host is None
            else tr(
                "Gateway: {p0}@{p1}:{p2}",
                p0=f"{route.username}",
                p1=f"{route.host}",
                p2=f"{route.port}",
            )
        )
        message.setText(
            tr(
                "Destination: {p0}:{p1}\nLogin: {p2}\n{p3}\n\nOpen this immutable Embedded SSH snapshot? The chosen config is not watched or inherited later. No saved profiles, credentials or trust files were changed.",
                p0=f"{profile.host}",
                p1=f"{profile.port}",
                p2=f"{profile.username or 'Enter in terminal'}",
                p3=f"{via}",
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        message.setDefaultButton(QMessageBox.StandardButton.Cancel)
        self._dialog = message
        try:
            accepted = message.exec() == QMessageBox.StandardButton.Ok
            if accepted and not self._closed and self._available():
                self._opener(profile)
        finally:
            self._dialog = None
            if isValid(message):
                message.deleteLater()

    def stop(self):
        self._closed = True
        if self._dialog is not None and isValid(self._dialog):
            self._dialog.reject()
