"""Signed checks and staged installation; normal exit remains owned by MainWindow."""

from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

from PySide6.QtCore import Qt, QThreadPool, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout

from termana.build_info import application_version
from termana.credentials.background_operation import BackgroundOperation
from termana.paths import application_config_directory
from termana.updates.client import fetch_public_metadata
from termana.updates.discovery import discover_update
from termana.updates.manifest import UpdateError
from termana.updates.official import PUBLIC_KEY, REPOSITORY, native_target
from termana.updates.state import UpdateStateStore


def check_official_update(directory, cancelled):
    def fetch(url, limit):
        if cancelled.is_set():
            raise UpdateError("Update check cancelled")
        data = fetch_public_metadata(url, limit)
        if cancelled.is_set():
            raise UpdateError("Update check cancelled")
        return data

    system, architecture = native_target()
    store = UpdateStateStore(directory)
    checkpoint = store.load(REPOSITORY, "stable", PUBLIC_KEY)
    result = discover_update(
        REPOSITORY,
        PUBLIC_KEY,
        channel="stable",
        current_version=application_version(),
        system=system,
        architecture=architecture,
        now=datetime.now(UTC),
        checkpoint=checkpoint,
        fetch=fetch,
    )
    if cancelled.is_set():
        raise UpdateError("Update check cancelled")
    store.accept(result.checkpoint, PUBLIC_KEY)
    return result


class UpdateDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        directory=None,
        checker=check_official_update,
        starter=None,
        opener=QDesktopServices.openUrl,
        installer=None,
        stager=None,
        target_provider=None,
    ):
        super().__init__(parent)
        self.setObjectName("updateDialog")
        self.setWindowTitle("Termana updates")
        self.resize(560, 290)
        self.directory = directory
        self.checker = checker
        self.starter = starter or QThreadPool.globalInstance().start
        self.opener = opener
        from termana.updates.installer import installed_target, stage_update

        self.installer = installer
        self.stager = stager or stage_update
        self.target_provider = target_provider or installed_target
        self.result = None
        self.stage_job = None
        self.pending = None
        self.cancelled = Event()
        self.release_url = None
        layout = QVBoxLayout(self)
        self.status = QLabel("Check for a newer Termana release.", self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        for text in (
            "Signed updates from GitHub. Your connection data stays local.",
            "Download first, then restart when ready. A backup is kept. Source launches update from source.",
        ):
            label = QLabel(text, self)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
        layout.addStretch()
        buttons = QDialogButtonBox(self)
        close_button = QPushButton("Close", self)
        close_button.setObjectName("closeUpdateButton")
        close_button.setProperty("actionRole", "secondary")
        close_button.setAutoDefault(False)
        buttons.addButton(close_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.check_button = QPushButton("Check again", self)
        self.check_button.setProperty("actionRole", "secondary")
        self.check_button.setAutoDefault(False)
        self.release_button = QPushButton("Download page", self)
        self.release_button.setProperty("actionRole", "secondary")
        self.release_button.setAutoDefault(False)
        self.release_button.setEnabled(False)
        self.install_button = QPushButton("Download update", self)
        self.install_button.setProperty("actionRole", "primary")
        self.install_button.setAutoDefault(False)
        self.install_button.setEnabled(False)
        buttons.addButton(self.check_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.release_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.install_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        self.check_button.clicked.connect(self.check)
        self.release_button.clicked.connect(self.open_release)
        self.install_button.clicked.connect(self.install)
        layout.addWidget(buttons)

    def check(self):
        if self.pending is not None:
            return
        self.cancelled = Event()
        cancelled = self.cancelled
        directory = self.directory or application_config_directory() / "updates"
        checker = self.checker

        def operation():
            try:
                return checker(directory, cancelled)
            except UpdateError as error:
                return str(error)  # Fixed application diagnostics, never server payloads.

        identity = uuid4()
        task = BackgroundOperation(identity, operation)
        self.pending = identity, task
        task.signals.succeeded.connect(self.completed)
        task.signals.failed.connect(self.failed)
        self.release_url = None
        self.result = None
        self.stage_job = None
        self.install_button.setText("Download update")
        self.install_button.setEnabled(False)
        self.release_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.status.setText("Checking signed release metadata…")
        try:
            self.starter(task)
        except Exception:
            self.failed(identity)

    @Slot(object, object)
    def completed(self, identity, result):
        if self.pending is None or self.pending[0] != identity:
            return
        self.pending = None
        self.check_button.setEnabled(True)
        if self.cancelled.is_set():
            self.status.setText("Update check cancelled.")
            return
        if isinstance(result, str):
            self.status.setText(result)
            return
        if result.status == "no-compatible-package":
            self.status.setText("No package is available for this operating system and processor.")
            return
        self.status.setText(
            f"Termana {result.manifest.version} is available."
            if result.status == "available"
            else f"Termana {application_version()} is up to date."
        )
        self.release_url = (
            f"https://github.com/{REPOSITORY.identity}/releases/tag/v{result.manifest.version}"
        )
        self.release_button.setEnabled(True)
        self.result = result
        if result.status == "available" and self.installer is not None:
            try:
                self.target_provider()
            except UpdateError as error:
                self.install_button.setToolTip(str(error))
            else:
                self.install_button.setEnabled(True)

    def install(self):
        if self.pending is not None or self.result is None:
            return
        if self.stage_job is not None:
            if self.installer is not None:
                self.installer(self.stage_job)
            return
        try:
            target = self.target_provider()
        except UpdateError as error:
            self.status.setText(str(error))
            return
        cancelled, stager, result = Event(), self.stager, self.result
        self.cancelled = cancelled

        def operation():
            try:
                return stager(result, target, cancelled)
            except UpdateError as error:
                return str(error)

        identity = uuid4()
        task = BackgroundOperation(identity, operation)
        self.pending = identity, task
        task.signals.succeeded.connect(self.staged)
        task.signals.failed.connect(self.stage_failed)
        self.install_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.status.setText(
            "Downloading and verifying the complete application… You can cancel safely."
        )
        try:
            self.starter(task)
        except Exception:
            self.stage_failed(identity)

    @Slot(object, object)
    def staged(self, identity, result):
        if self.pending is None or self.pending[0] != identity:
            return
        self.pending = None
        self.check_button.setEnabled(True)
        if self.cancelled.is_set():
            self.status.setText("Download cancelled. The installed application was not changed.")
        elif isinstance(result, str):
            self.status.setText(result)
            self.install_button.setEnabled(True)
        else:
            self.stage_job = result
            self.install_button.setText("Restart and install")
            self.install_button.setEnabled(True)
            self.status.setText(
                "Update verified and ready. Restart and install when your sessions can be closed."
            )

    @Slot(object)
    def stage_failed(self, identity):
        self.staged(
            identity,
            "Could not prepare the update securely. The installed application was not changed.",
        )

    @Slot(object)
    def failed(self, identity):
        self.completed(identity, "Could not check updates securely. Try again later.")

    def open_release(self):
        if self.release_url is not None and self.release_button.isEnabled():
            self.opener(QUrl(self.release_url))

    def done(self, result):
        self.cancelled.set()
        super().done(result)
