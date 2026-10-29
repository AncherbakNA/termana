"""Explicit reusable-account CRUD without reading a password into the UI."""

from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from termana.credentials.repository import SshCredentialRepositoryError
from termana.credentials.ssh_auth import SshCredentialMutationOutcome
from termana.dialog_geometry import fit_dialog_to_screen
from termana.models.ssh_account import SshAccount
from termana.qt_lifecycle import schedule_delete_later


class SshAccountsDialog(QDialog):
    def __init__(self, repository, coordinator, parent=None):
        super().__init__(parent)
        self.setObjectName("sshAccountsDialog")
        self.setWindowTitle("SSH accounts")
        self.resize(560, 540)
        self.setMinimumSize(420, 400)
        self.setSizeGripEnabled(True)
        self._repository, self._coordinator = repository, coordinator
        self._pending = None
        self._accounts = ()
        self._selected = None
        self._disposed = False
        self._confirmation = None
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(96)
        body = QWidget(scroll)
        body.setMinimumWidth(0)
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self.body_scroll = scroll
        account_details = (
            "Create an account, then select it in a connection profile. Passwords stay in "
            "the system vault. Linking a profile authorizes this account only for that "
            "profile's destination; gateway login remains separate."
        )
        notice = QLabel("Create accounts for saved connections. Passwords stay in the system vault.")
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setToolTip(account_details)
        notice.setAccessibleDescription(account_details)
        content.addWidget(notice)
        self.list = QListWidget()
        self.list.setObjectName("sshAccountsList")
        content.addWidget(self.list, 1)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.name = QLineEdit()
        self.name.setMaxLength(128)
        self.name.setObjectName("sshAccountNameInput")
        self.username = QLineEdit()
        self.username.setMaxLength(128)
        self.username.setObjectName("sshAccountUsernameInput")
        self.password = QLineEdit()
        self.password.setObjectName("sshAccountPasswordInput")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(65536)
        for control in (self.name, self.username, self.password):
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        form.addRow("Account name", self.name)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        content.addLayout(form)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        content.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.new_button = QPushButton("New", self)
        self.remove_button = QPushButton("Remove", self)
        tools = QHBoxLayout()
        tools.addWidget(self.new_button)
        tools.addWidget(self.remove_button)
        tools.addStretch(1)
        content.insertLayout(1, tools)
        self.save_button = buttons.addButton("Save account", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setDefault(True)
        self.save_button.setProperty("actionRole", "primary")
        buttons.button(QDialogButtonBox.StandardButton.Close).setProperty("actionRole", "secondary")
        for button in (self.new_button, self.remove_button):
            button.setProperty("actionRole", "secondary")
            button.setAutoDefault(False)
        root.addWidget(buttons)
        buttons.rejected.connect(self.reject)
        self.new_button.clicked.connect(self._new)
        self.save_button.clicked.connect(self._save)
        self.remove_button.clicked.connect(self._remove)
        self.list.currentRowChanged.connect(self._select)
        coordinator.operation_finished.connect(self._finished)
        self.finished.connect(self._dispose)
        try:
            self._refresh()
        except SshCredentialRepositoryError:
            self.status.setText(
                "SSH accounts are unavailable or busy. Close and reopen this window."
            )
            self.save_button.setEnabled(False)
            self.remove_button.setEnabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self, lambda: fit_dialog_to_screen(self))

    def _dispose(self, *_):
        self.password.clear()
        if not self._disposed:
            self._disposed = True
            if self._confirmation is not None:
                self._confirmation.reject()
            self._coordinator.operation_finished.disconnect(self._finished)

    def _refresh(self):
        self._accounts = self._repository.accounts()
        self.list.clear()
        for account in self._accounts:
            self.list.addItem(f"{account.name} — {account.username}")
        self._new()

    def _new(self):
        self.list.setCurrentRow(-1)
        self._selected = None
        self.name.clear()
        self.username.clear()
        self.password.clear()
        self.password.setPlaceholderText("Stored in the system vault when you save")
        self.name.setFocus()

    def _select(self, row):
        if not 0 <= row < len(self._accounts):
            return
        self._selected = self._accounts[row]
        self.name.setText(self._selected.name)
        self.username.setText(self._selected.username)
        self.password.clear()
        self.password.setPlaceholderText("Leave blank to keep the stored password")

    def _save(self):
        if self._disposed or self._pending is not None:
            return
        try:
            values = dict(name=self.name.text().strip(), username=self.username.text().strip())
            account = (
                SshAccount(**values)
                if self._selected is None
                else replace(self._selected, **values)
            )
        except ValueError as error:
            self.status.setText(str(error))
            return
        password = self.password.text()
        self.password.clear()
        self._start(account, None if self._selected is not None and not password else password)

    def _remove(self):
        if self._disposed or self._selected is None or self._pending is not None:
            return
        account = self._selected
        box = QMessageBox(self)
        box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        box.setObjectName("removeSshAccountDialog")
        box.setProperty("dialogRole", "actionNotice")
        box.setWindowTitle("Remove SSH account?")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText("Remove this account and its vault password?")
        box.setInformativeText(
            "Linked profiles will require another account. Existing connected terminals stay open."
        )
        remove = box.addButton("Remove account", QMessageBox.ButtonRole.DestructiveRole)
        remove.setProperty("actionRole", "danger")
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        cancel.setProperty("actionRole", "secondary")
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        self._confirmation = box
        try:
            box.exec()
            if box.clickedButton() is remove and not self._disposed:
                self._start(account, remove=True)
        finally:
            self._confirmation = None
            schedule_delete_later(box)

    def _start(self, account, password=None, *, remove=False):
        if self._disposed or self._pending is not None:
            return
        self._pending = uuid4()
        for control in (
            self.list,
            self.name,
            self.username,
            self.password,
            self.new_button,
            self.remove_button,
            self.save_button,
        ):
            control.setEnabled(False)
        self.status.setText("Updating the system vault…")
        try:
            self._coordinator.start_account_change(
                self._pending, self._repository, account, password, remove=remove
            )
        except Exception:
            self._finished(self._pending, SshCredentialMutationOutcome.FAILED)

    def _finished(self, operation_id, outcome):
        if operation_id != self._pending:
            return
        self._pending = None
        for control in (
            self.list,
            self.name,
            self.username,
            self.password,
            self.new_button,
            self.remove_button,
            self.save_button,
        ):
            control.setEnabled(True)
        try:
            self._refresh()
        except Exception:
            self.status.setText("Account metadata is unavailable. Close and reopen this window.")
            return
        self.status.setText(
            "Account updated."
            if outcome
            in (
                SshCredentialMutationOutcome.SAVE_SUCCEEDED,
                SshCredentialMutationOutcome.REMOVE_SUCCEEDED,
            )
            else "Could not update the account. The vault may be locked or busy; try again."
        )
