"""Optional explicit trust/vault UI; attempt-fenced, no confidential Qt payloads."""
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QThreadPool, Slot
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget

from termana.credentials.background_operation import (
    BackgroundOperation,
    ConfidentialBackgroundOperation,
)
from termana.i18n import tr
from termana.rdp.certificate import display_fingerprint
from termana.rdp.secret_store import RdpLogin


class RdpIdentityServices(QObject):
    def __init__(self, page, *, vault=None, trust_store=None, repository=None):
        super().__init__(page)
        self.page, self.vault, self.trust_store = page, vault, trust_store
        self.repository = repository
        self._closed = False
        self._tasks = {}
        self._review = None
        self._trusted = False
        self._bypass_saved = False
        self._revision = 0
        self._pending_login = None
        self._trust_job = self._read_job = None
        if repository is not None:
            repository.operation_finished.connect(self._operation_returned)
        self.trust_remember = QCheckBox(tr("Remember this certificate"), page.trust)
        self.trust_remember.setVisible(trust_store is not None)
        self.trust_remember.setEnabled(False)
        page.trust_layout.insertWidget(3, self.trust_remember)
        self.trust_remember.toggled.connect(
            lambda checked: page.trust_once.setText(tr("Connect and remember") if checked else tr("Connect once")))
        self.controls = QWidget(page.login)
        layout = QVBoxLayout(self.controls)
        layout.setContentsMargins(0, 0, 0, 0)
        self.remember = QCheckBox(tr("Remember sign-in"), self.controls)
        self.use_saved = QPushButton(tr("Use saved sign-in"), self.controls)
        self.forget = QPushButton(tr("Forget saved sign-in"), self.controls)
        self.hint = QLabel(self.controls)
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.remember)
        for button in (self.use_saved, self.forget):
            button.setProperty("actionRole", "secondary")
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.hint)
        page.login_layout.insertWidget(3, self.controls)
        self.controls.hide()
        self.use_saved.clicked.connect(self.read_saved)
        self.forget.clicked.connect(self.forget_saved)
        for edit in (page.username, page.domain, page.password):
            edit.textChanged.connect(self._edited)

    def _edited(self):
        self._revision += 1

    @Slot(object)
    def _operation_returned(self, operation):
        # Abandoned confidential tasks intentionally have no UI-result signal.
        # Release their retired page references after the actual worker returns.
        self._tasks.pop(operation,None)

    def _launch(self, operation, task):
        if self.repository is not None:
            self.repository.start(operation,task)
        else:
            QThreadPool.globalInstance().start(task)

    def _abandon_reads(self):
        if self._read_job in self._tasks:
            task = self._tasks[self._read_job][0]
            task.abandon(self._read_job)
        self._read_job = self._trust_job = None

    def start(self):
        self._abandon_reads()
        self._review = self._pending_login = None
        self._trusted = False
        self.trust_remember.setChecked(False)
        self.remember.setChecked(False)
        self.hint.clear()

    def other_user(self):
        self._bypass_saved = True
        self._abandon_reads()
        self._pending_login = None

    def certificate(self):
        page, c = self.page, self.page.controller
        details = c.certificate_details
        self._review = None
        self.trust_remember.setChecked(False)
        self.trust_remember.setEnabled(False)
        if details is None:
            return
        page.certificate_text.setText(page.certificate_text.text()
            + "\n" + tr("Valid until") + ": " + details.not_after.strftime("%Y-%m-%d %H:%M UTC")
            + ("\n"+tr("Expired or not yet valid. It cannot be remembered.") if not details.currently_valid() else ""))
        if self.trust_store is None:
            return
        operation = uuid4()
        owner, request = c.owner, page._request
        store, host, port = self.trust_store, c.host, c.port
        task = BackgroundOperation(operation, lambda: store.review(host, port, details))
        self._trust_job = operation
        self._tasks[operation] = (task, (owner, request))
        task.signals.succeeded.connect(self._reviewed)
        task.signals.failed.connect(self._failed)
        self._launch(operation,task)

    @Slot(object, object)
    def _reviewed(self, operation, review):
        record = self._tasks.pop(operation, None)
        page, c = self.page, self.page.controller
        if (not record or self._closed or operation != self._trust_job
                or record[1] != (c.owner, page._request) or c.state != "Certificate review"):
            return
        self._trust_job = None
        self._review = review
        details = c.certificate_details
        if review.matches and details.remember_allowed:
            self._trusted = True
            if c.answer_certificate(page._request, True):
                page.stack.setCurrentWidget(page.desktop_view)
            return
        if review.fingerprint and review.fingerprint != details.fingerprint:
            page.certificate_text.setText(tr("Server certificate changed. Verify with the server owner.")
                + "\n"+page.certificate_text.text()+"\n"+tr("Previously trusted SHA256")+":\n"
                + display_fingerprint(review.fingerprint))
            self.trust_remember.setText(tr("Replace saved certificate"))
        else:
            self.trust_remember.setText(tr("Remember this certificate"))
        self.trust_remember.setEnabled(details.remember_allowed)

    def accept_certificate(self):
        page, c = self.page, self.page.controller
        if self._closed or c.state != "Certificate review":
            return False
        if self.trust_remember.isChecked():
            if self._review is None or self._trust_job is not None:
                return False
            try:
                self.trust_store.remember(self._review, c.certificate_details)
            except Exception:
                page.certificate_text.setText(tr("Certificate could not be saved safely. Uncheck Remember to connect once."))
                return False
            self._trusted = True
        return c.answer_certificate(page._request, True)

    def credentials(self):
        c = self.page.controller
        # No callback means native CA/name verification succeeded. A new or
        # changed certificate accepted once never unlocks an old saved password.
        if c.certificate_details is None:
            self._trusted = True
        enabled = bool(self.vault is not None and self.page.profile is not None)
        self.controls.setVisible(enabled)
        self.use_saved.setEnabled(enabled and self._trusted and not self._bypass_saved)
        self.remember.setEnabled(enabled and self._trusted and not self._bypass_saved)
        self.forget.setEnabled(enabled)
        self.remember.setChecked(False)
        self.hint.setText(tr("Saved sign-ins stay locked for a one-time certificate exception.")
                          if enabled and not self._trusted else "")

    @Slot()
    def read_saved(self):
        page, c = self.page, self.page.controller
        if (self._closed or self._bypass_saved or not self._trusted or self._read_job
                or self.vault is None or page.profile is None or c.state != "Sign in"):
            return
        operation = uuid4()
        vault, profile = self.vault, page.profile
        task = ConfidentialBackgroundOperation(operation, lambda: vault.get_login(profile))
        self._read_job = operation
        self._tasks[operation] = (task, (c.owner, page._request, self._revision, profile))
        self.use_saved.setEnabled(False)
        task.signals.succeeded.connect(self._loaded)
        task.signals.failed.connect(self._failed)
        self._launch(operation,task)

    @Slot(object)
    def _loaded(self, operation):
        record = self._tasks.pop(operation, None)
        if record is None:
            return
        task, context = record
        page, c = self.page, self.page.controller
        if (self._closed or operation != self._read_job or not self._trusted
                or self._bypass_saved or page.profile is None or self.vault.is_revoked(page.profile) or c.state != "Sign in"
                or context != (c.owner, page._request, self._revision, page.profile)):
            task.abandon(operation)
            if operation == self._read_job:
                self._read_job = None
                self.use_saved.setEnabled(not self._closed and self._trusted and not self._bypass_saved)
            return
        self._read_job = None
        login = task.take_result(operation)
        try:
            if login is None:
                self.hint.setText(tr("No saved sign-in for this profile. Enter it manually."))
            elif type(login) is RdpLogin:
                page.username.setText(login.username)
                page.domain.setText(login.domain)
                page.password.setText(login.password)
                self.hint.setText(tr("Saved sign-in loaded. Click Sign in to connect."))
        finally:
            login = None
            self.use_saved.setEnabled(True)

    def submitted(self, username, domain, password):
        self._pending_login = None
        if (not self._closed and self._trusted and self.vault is not None and self.page.profile is not None
                and self.remember.isChecked() and not self._bypass_saved):
            try:
                self._pending_login = self.page.controller.owner, self.page.profile, RdpLogin(username, domain, password)
            except Exception:
                self.hint.setText(tr("This sign-in cannot be saved. The connection is unchanged."))

    def state(self, state):
        if state == "Connected" and self._pending_login is not None:
            owner, profile, login = self._pending_login
            self._pending_login = None
            if not self._closed and owner == self.page.controller.owner and profile == self.page.profile:
                operation, vault = uuid4(), self.vault
                task = BackgroundOperation(operation, lambda: vault.save_login(profile, login))
                self._tasks[operation] = (task, ("save", owner))
                task.signals.succeeded.connect(self._saved)
                task.signals.failed.connect(self._failed)
                self._launch(operation,task)
        elif state in {"Failed", "Disconnecting"} or state.startswith("Session ended"):
            self._pending_login = None
            self._abandon_reads()

    @Slot()
    def forget_saved(self):
        page = self.page
        if self._closed or self.vault is None or page.profile is None or page.controller.state != "Sign in":
            return
        # This explicit button removes only this profile's RDP item, never SSH.
        operation, vault, profile = uuid4(), self.vault, page.profile
        task = BackgroundOperation(operation, lambda: vault.forget_login(profile))
        self._abandon_reads()
        page.password.clear()
        self.remember.setChecked(False)
        self._tasks[operation] = (task, ("forget", page.controller.owner))
        task.signals.succeeded.connect(self._saved)
        task.signals.failed.connect(self._failed)
        self._launch(operation,task)

    @Slot(object, object)
    def _saved(self, operation, _result):
        record = self._tasks.pop(operation, None)
        if record and not self._closed and record[1][1] == self.page.controller.owner:
            self.hint.setText(tr("Sign-in saved in the system vault.") if record[1][0] == "save"
                              else tr("Saved sign-in removed."))

    @Slot(object)
    def _failed(self, operation):
        record = self._tasks.pop(operation, None)
        if not record or self._closed:
            return
        if operation == self._trust_job:
            self._trust_job = None
            self.page.certificate_text.setText(self.page.certificate_text.text()+"\n"
                + tr("Saved trust is unavailable. You can still connect once."))
        elif operation == self._read_job:
            self._read_job = None
            self.use_saved.setEnabled(self._trusted and not self._bypass_saved)
            self.hint.setText(tr("Saved sign-in unavailable. Enter it manually."))
        elif record[1][0] in {"save", "forget"} and record[1][1] == self.page.controller.owner:
            self.page.connection_notice.setText(tr("The system vault could not complete the request. The connection is unchanged."))
            self.page.connection_notice.show()

    def shutdown(self):
        self._closed = True
        self._pending_login = None
        self._abandon_reads()
