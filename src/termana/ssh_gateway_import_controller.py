"""Explicit, reviewed local gateway import; never opens a connection or copies keys."""

from uuid import uuid4

from PySide6.QtCore import QObject, QThreadPool, Slot
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog
from shiboken6 import isValid

from termana.credentials.background_operation import BackgroundOperation
from termana.dialogs.ssh_gateway_import_dialog import SshGatewayImportDialog
from termana.i18n import tr
from termana.terminal.system_ssh_config import import_gateway


class SshGatewayImportController(QObject):
    def __init__(self, window, store, available, operation_starter=None):
        super().__init__(window)
        self.window, self.store, self.available = window, store, available
        self.starter = operation_starter or QThreadPool.globalInstance().start
        self.pending, self.dialog, self.closed, self.choosing = None, None, False, False

    def _choose(self, dialog, result):
        self.dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            return result(dialog) if accepted and not self.closed and self.available() else None
        finally:
            self.dialog = None
            if isValid(dialog):
                dialog.deleteLater()

    def _start(self, operation, slot):
        identity = uuid4()
        task = BackgroundOperation(identity, operation)
        self.pending = identity, task
        task.signals.succeeded.connect(slot)
        task.signals.failed.connect(self._failed)
        try:
            self.starter(task)
        except Exception:
            self._failed(identity)

    def open(self, *_):
        if (
            self.closed
            or self.pending is not None
            or self.choosing
            or self.dialog is not None
            or not self.available()
        ):
            return
        self.choosing = True
        try:
            picker = QFileDialog(self.window, tr("Import gateway SSH settings"))
            picker.setFileMode(QFileDialog.FileMode.ExistingFile)
            filename = self._choose(picker, lambda d: next(iter(d.selectedFiles()), None))
            if not filename:
                return
            prompt = QInputDialog(self.window)
            prompt.setWindowTitle(tr("Gateway alias"))
            prompt.setLabelText(tr("Gateway Host alias from the selected file"))
            alias = self._choose(prompt, lambda d: d.textValue())
            if alias is None:
                return
            from pathlib import Path

            def resolve():
                return import_gateway(Path(filename).absolute(), alias), self.store.load()

            self._start(resolve, self._resolved)
        finally:
            self.choosing = False

    def _claim(self, identity):
        if self.pending is None or self.pending[0] != identity:
            return False
        self.pending = None
        return not self.closed and self.available()

    @Slot(object)
    def _failed(self, identity):
        if not self._claim(identity):
            return
        self.window.statusBar().showMessage(
            tr(
                "Gateway import refused or could not be saved safely. Supported: Host/HostName/Port/User, IdentityFile, IdentitiesOnly, IdentityAgent and connection timers. No Include, Match, commands, nested gateways, trust overrides or tokens. Source config and keys were not changed; review Termana ssh_config and import again."
            ),
            15000,
        )

    @Slot(object, object)
    def _resolved(self, identity, result):
        if not self._claim(identity):
            return
        gateway, catalog = result
        replacing = any(g.alias.casefold() == gateway.alias.casefold() for g in catalog.gateways)
        review = SshGatewayImportDialog(gateway, replacing=replacing, parent=self.window)
        self.dialog = review
        try:
            accepted = review.exec() == QDialog.DialogCode.Accepted
            if accepted and not self.closed and self.available():
                self._start(
                    lambda: self.store.save_gateway(gateway, expected_revision=catalog.revision),
                    self._saved,
                )
        finally:
            self.dialog = None
            if isValid(review):
                review.deleteLater()

    @Slot(object, object)
    def _saved(self, identity, _result):
        if self._claim(identity):
            self.window.statusBar().showMessage(
                tr(
                    "Gateway settings imported into Termana ssh_config. Use this alias in the jump-host field. Changes apply only to new connections; source config and keys are unchanged."
                ),
                15000,
            )

    def stop(self):
        self.closed = True
        if self.dialog is not None and isValid(self.dialog):
            self.dialog.reject()
