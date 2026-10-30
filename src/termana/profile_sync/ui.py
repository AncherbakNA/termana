"""Opt-in, modeless sync owner. Credentials travel through one-shot mailboxes."""

from threading import Event
from uuid import uuid4

from PySide6.QtCore import Qt, QThreadPool, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from termana.credentials.background_operation import ConfidentialBackgroundOperation
from termana.profile_sync.application import prepare_application
from termana.profile_sync.conflicts import SyncConflictReport
from termana.profile_sync.github import GitHubCatalog, begin_login, finish_login
from termana.profile_sync.merge import SyncMergeResult
from termana.profile_sync.model import SyncError, project_catalog
from termana.profile_sync.service import (
    SyncSecrets,
    native_vault,
    new_binding,
    restore_credentials,
    synchronize,
)
from termana.updates.manifest import ReleaseRepository, UpdateError


class SyncDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        store,
        catalog,
        commit,
        disconnect=None,
        reload_catalog=None,
        vault=None,
        starter=None,
        opener=QDesktopServices.openUrl,
        client=GitHubCatalog,
        begin=begin_login,
        finish=finish_login,
    ):
        super().__init__(parent)
        self.setObjectName("githubSyncDialog")
        self.setWindowTitle("GitHub session sync")
        self.resize(600, 430)
        self.store, self.catalog, self.commit = store, catalog, commit
        self.disconnect = disconnect
        self.reload_catalog = reload_catalog
        self.vault = vault or native_vault()
        self.starter = starter or QThreadPool.globalInstance().start
        self.opener, self.client, self.begin, self.finish = opener, client, begin, finish
        self.pending, self.cancelled, self._token = None, Event(), None
        self._close_requested = False
        self._conflict = None
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        hint = QLabel(
            "Encrypted sync of connections, folders and colors. Passwords, keys and host trust stay local.",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        access = QLabel(
            '<a href="https://github.com/apps/termana-sync/installations/new">Set up GitHub access</a> — select only your private sync repository.',
            self,
        )
        access.setWordWrap(True)
        access.linkActivated.connect(
            lambda _: self.opener(QUrl("https://github.com/apps/termana-sync/installations/new"))
        )
        layout.addWidget(access)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.repository = QLineEdit(self)
        self.repository.setPlaceholderText("your-account/termana-sync")
        self.recovery = QLineEdit(self)
        self.recovery.setEchoMode(QLineEdit.EchoMode.Password)
        self.recovery.setPlaceholderText("Leave empty on the first computer")
        form.addRow("Private repository", self.repository)
        form.addRow("Recovery key", self.recovery)
        layout.addLayout(form)
        self.saved_key = QCheckBox("Recovery key saved safely", self)
        self.saved_key.setToolTip(
            "Keep a copy outside this computer. You need it to add a device or recover sync."
        )
        layout.addWidget(self.saved_key)
        self.status = QLabel("Sign in, then connect a private repository.", self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        layout.addStretch()
        box = QHBoxLayout()
        box.addStretch()
        box.setSpacing(12)
        self.login_button = self._button(box, "Sign in", self.login)
        self.connect_button = self._button(box, "Connect", self.connect_repository)
        self.sync_button = self._button(box, "Sync now", self.sync)
        self.reload_button = self._button(box, "Reload local data", self.reload_local)
        self.sync_button.setProperty("actionRole", "primary")
        layout.addLayout(box)
        secondary = QHBoxLayout()
        secondary.addStretch()
        secondary.setSpacing(12)
        self.key_button = self._button(secondary, "Recovery key", self.show_key)
        self.resolve_button = self._button(secondary, "Resolve…", self.resolve_conflict)
        self.disconnect_button = self._button(secondary, "Disconnect", self.disconnect_repository)
        self.close_button = self._button(secondary, "Close", self.reject)
        layout.addLayout(secondary)
        if self.store.sync_state is not None:
            self.repository.setText(self.store.sync_state.repository.identity)
            self.repository.setReadOnly(True)
            self.saved_key.setChecked(self.store.sync_state.base is not None)
            self.status.setText(
                "Connected. Sync now to upload local edits and download remote changes."
            )
        self._refresh()

    def _button(self, box, text, callback):
        button = QPushButton(text, self)
        button.setProperty("actionRole", "secondary")
        button.setAutoDefault(False)
        button.clicked.connect(callback)
        box.addWidget(button)
        return button

    def _refresh(self):
        idle, bound = self.pending is None, self.store.sync_state is not None
        self.login_button.setEnabled(idle)
        self.connect_button.setText("Recover" if bound else "Connect")
        self.connect_button.setEnabled(idle and self._token is not None)
        self.sync_button.setEnabled(idle and bound)
        self.reload_button.setEnabled(idle and self.reload_catalog is not None)
        self.key_button.setEnabled(idle and bound)
        self.resolve_button.setEnabled(idle and bound and self._conflict is not None)
        self.disconnect_button.setEnabled(idle and bound and self.disconnect is not None)
        self.repository.setEnabled(idle and not bound)
        self.recovery.setEnabled(idle)
        self.recovery.setPlaceholderText(
            "Enter your backup key if saved credentials are missing"
            if bound
            else "Leave empty on the first computer"
        )

    def _start(self, kind, operation):
        if self.pending is not None:
            return
        self.cancelled = Event()
        identity = uuid4()
        cancelled = self.cancelled

        def run():
            try:
                return True, operation(cancelled)
            except SyncError as error:
                return False, error if isinstance(error, SyncConflictReport) else str(error)
            except Exception:
                return (
                    False,
                    "Could not complete secure sync. Local connections were not replaced; retry or sign in again.",
                )

        task = ConfidentialBackgroundOperation(identity, run)
        self.pending = identity, task, kind
        task.signals.succeeded.connect(self._completed)
        task.signals.failed.connect(self._failed)
        self._refresh()
        try:
            self.starter(task)
        except Exception:
            self._failed(identity)

    def login(self):
        self.status.setText("Requesting GitHub sign-in code…")
        self._start("code", lambda _: self.begin())

    def connect_repository(self):
        state = self.store.sync_state
        if state is not None:
            token, recovery = self._token, self.recovery.text().strip()
            client, vault = self.client(state.repository, token), self.vault
            self.status.setText("Restoring authorization for this device…")
            self._start(
                "reauth", lambda _: restore_credentials(state, token, recovery, vault, client)
            )
            return
        try:
            repository = ReleaseRepository.parse(self.repository.text().strip())
        except UpdateError:
            self.status.setText("Enter the private repository as owner/name.")
            return
        catalog, token, recovery = self.catalog(), self._token, self.recovery.text().strip()
        self._binding_source = catalog
        client = self.client(repository, token)
        self.status.setText("Validating repository and saving the key in the system vault…")
        self._start(
            "connect",
            lambda _: new_binding(
                catalog, client, SyncSecrets(token, recovery), self.vault, recovery=recovery
            ),
        )

    def sync(self, _checked=False, *, resolution=None):
        state, catalog = self.store.sync_state, self.catalog()
        if state is None:
            return
        if not self.saved_key.isChecked():
            self.status.setText(
                "Save your recovery key first. It is needed to connect another computer or recover access."
            )
            return
        vault, factory = self.vault, self.client

        def operation(cancelled):
            secrets = SyncSecrets.load(vault, state.secret_id)
            return synchronize(
                catalog,
                state,
                secrets,
                factory(state.repository, secrets.token),
                cancelled,
                resolution=resolution,
            )

        self.status.setText("Synchronizing encrypted connections…")
        self._start("sync", operation)

    def resolve_conflict(self):
        if self._conflict is None or self.pending is not None:
            return
        from termana.profile_sync.conflict_dialog import ConflictDialog

        report = self._conflict
        dialog = ConflictDialog(report, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.sync(resolution=(report.fingerprint, dialog.selection))
        finally:
            dialog.deleteLater()

    def show_key(self):
        if self.recovery.echoMode() == QLineEdit.EchoMode.Normal and self.recovery.text():
            self.recovery.clear()
            self.recovery.setReadOnly(False)
            self.recovery.setEchoMode(QLineEdit.EchoMode.Password)
            self.key_button.setText("Recovery key")
            self._refresh()
            return
        state, vault = self.store.sync_state, self.vault
        if state is not None:
            self._start("key", lambda _: SyncSecrets.load(vault, state.secret_id).recovery)

    def disconnect_repository(self):
        from PySide6.QtWidgets import QMessageBox

        state = self.store.sync_state
        if state is None or self.disconnect is None or self.pending is not None:
            return
        if (
            QMessageBox.question(
                self,
                "Disconnect sync?",
                "Keep all local connections and the private GitHub repository, but remove this computer's saved sync credentials and key?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.disconnect()
        except Exception:
            self.status.setText("Could not disconnect safely. Reload connections before retrying.")
            return
        vault = self.vault
        self._start("disconnect", lambda _: vault.delete_secret(state.secret_id))

    def reload_local(self):
        if self.pending is not None or self.reload_catalog is None:
            return
        previous = self.store.sync_state
        try:
            self.reload_catalog()
            state = self.store.sync_state
            self.recovery.clear()
            self.recovery.setEchoMode(QLineEdit.EchoMode.Password)
            self.key_button.setText("Recovery key")
            self.repository.setReadOnly(state is not None)
            if state is not None:
                self.repository.setText(state.repository.identity)
            if previous is None or state is None or previous.secret_id != state.secret_id:
                self.saved_key.setChecked(state is not None and state.base is not None)
            self._conflict = None
            self.status.setText("Saved local data reloaded. Active sessions were not changed.")
        except Exception:
            self.status.setText(
                "Could not reload saved connections. Keep your files and review the application data before retrying."
            )
        self._refresh()

    @Slot(object)
    def _completed(self, identity):
        if self.pending is None or self.pending[0] != identity:
            return
        _, task, kind = self.pending
        ok, result = task.take_result(identity)
        self.pending = None
        if not ok:
            if kind == "disconnect":
                self.status.setText(
                    "Disconnected locally, but the system-vault entry could not be removed. GitHub data and local connections were kept."
                )
                self._refresh()
                return
            if isinstance(result, SyncConflictReport):
                self._conflict = result
                self.status.setText(str(result))
            else:
                self.status.setText(result)
        elif kind == "code" and not self._close_requested:
            self.status.setText("Enter this code on GitHub: " + result.user_code)
            self.opener(QUrl("https://github.com/login/device"))
            self._start("token", lambda cancelled: self.finish(result, cancelled))
            return
        elif kind == "token" and not self._close_requested:
            state = self.store.sync_state
            self._token = result
            if state is None:
                self._token = result
                self.status.setText("Signed in. Select your private sync repository and connect.")
            else:
                token, vault = result, self.vault
                factory = self.client
                recovery = self.recovery.text().strip()

                def reauthorize(_):
                    return restore_credentials(
                        state, token, recovery, vault, factory(state.repository, token)
                    )

                self._start("reauth", reauthorize)
                return
        elif kind == "connect":
            state, _secrets = result
            try:
                local = self.catalog()
                if local != self._binding_source:
                    raise SyncError("Local connections changed while connecting")
                snapshot, _ = project_catalog(local, state.identities)
                plan = prepare_application(
                    local, state.identities, snapshot, SyncMergeResult(snapshot, ())
                )
                self.commit(plan, state)
                self.repository.setReadOnly(True)
                self.recovery.clear()
                self._token = None
                self.saved_key.setChecked(False)
                self.status.setText("Connected. Show and save the recovery key, then sync.")
            except Exception:
                self.status.setText(
                    "Could not save the sync binding. Reload connections before retrying."
                )
        elif kind == "sync":
            try:
                self.commit(result.plan, result.state)
                self._conflict = None
                self.status.setText("Synchronized. Active sessions were not changed.")
            except Exception:
                self.status.setText(
                    "Local connections changed or saving failed. Reload and sync again; the remote save may have completed."
                )
        elif kind == "key" and not self._close_requested:
            self.recovery.setText(result)
            self.recovery.setEchoMode(QLineEdit.EchoMode.Normal)
            self.recovery.setReadOnly(True)
            self.recovery.setEnabled(True)
            self.key_button.setText("Hide key")
            self.status.setText(
                "Save this key privately. Anyone with it and repository access can read your synced connections."
            )
        elif kind == "reauth":
            self._token = None
            self.recovery.clear()
            self.recovery.setReadOnly(False)
            self.recovery.setEchoMode(QLineEdit.EchoMode.Password)
            self.key_button.setText("Recovery key")
            self.saved_key.setChecked(True)
            self.status.setText("GitHub authorization renewed. Sync now when ready.")
        elif kind == "disconnect":
            self.saved_key.setChecked(False)
            self.repository.setReadOnly(False)
            self.recovery.clear()
            self.recovery.setReadOnly(False)
            self.recovery.setEchoMode(QLineEdit.EchoMode.Password)
            self._token = None
            self.status.setText(
                "Disconnected on this computer. Local connections and GitHub data were kept."
            )
        self._refresh()
        if kind == "key" and not self._close_requested:
            self.recovery.setEnabled(True)
        if self._close_requested:
            self.reject()

    @Slot(object)
    def _failed(self, identity):
        if self.pending is not None and self.pending[0] == identity:
            self.pending = None
            self.status.setText("Secure operation failed. Try again.")
            self._refresh()
            if self._close_requested:
                self.reject()

    def reject(self):
        self.cancelled.set()
        if self.pending is not None:
            self._close_requested = True
            self.status.setText("Finishing the current operation safely…")
            return
        self.recovery.clear()
        self.recovery.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_button.setText("Recovery key")
        self._token = None
        self._conflict = None
        self._close_requested = False
        super().reject()
