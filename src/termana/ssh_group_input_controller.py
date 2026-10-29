"""Explicit, ephemeral SSH keyboard broadcasting; never parser/mouse/auth input."""

from PySide6.QtCore import QObject, QSignalBlocker, Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QListWidget, QListWidgetItem, QVBoxLayout
from shiboken6 import isValid

from termana.i18n import tr
from termana.terminal.title import sanitize_terminal_title
from termana.widgets.terminal_page import TerminalPage
from termana.widgets.wrapping_label import WrappingLabel

MAX_GROUP_TARGETS = 16
MAX_GROUP_KEY_BYTES = 4096


class SshGroupInputController(QObject):
    def __init__(self, window, workspace, action):
        super().__init__(window)
        self._window, self._workspace, self._action = window, workspace, action
        self._targets = {}
        self._slots = {}
        self._tooltips = {}
        self._dialog = None
        action.setCheckable(True)
        action.triggered.connect(self.configure)
        workspace.open_sessions_changed.connect(self._check_owners)
        self._closed = False

    @staticmethod
    def eligible(page):
        try:
            from termana.terminal.embedded_ssh_backend import EmbeddedSshBackend
        except ImportError:
            return False
        return (
            isinstance(page, TerminalPage)
            and isValid(page)
            and isValid(page.backend)
            and isinstance(page.backend, EmbeddedSshBackend)
            and page.backend.sftp_available
            and page.command_insertion_available
            and not page.session_ended
        )

    def _check_owners(self, *_):
        current = self._workspace._all_pages()
        if any(
            page not in current or not self.eligible(page) or page.backend is not backend
            for page, backend in self._targets.items()
        ):
            self.disable(tr("Group input stopped: a selected SSH owner changed or ended."))

    def enable(self, pages):
        pages = tuple(pages)
        self.disable()
        if (
            self._closed
            or not 2 <= len(pages) <= MAX_GROUP_TARGETS
            or len(set(pages)) != len(pages)
        ):
            return False
        current = self._workspace._all_pages()
        if any(page not in current or not self.eligible(page) for page in pages):
            return False
        if len({page.backend for page in pages}) != len(pages):
            return False
        self._targets = {page: page.backend for page in pages}
        for page in pages:

            def slot(intent, page=page):
                self._broadcast(page, intent)

            self._slots[page] = slot
            self._tooltips[page] = page.output_view.toolTip()
            page.output_view.user_key_generated.connect(slot)
            page.group_input_stop_requested.connect(self._quick_stop)
            page.input_lock_changed.connect(self._check_owners)
            page.backend.finished.connect(self._check_owners)
            page.backend.destroyed.connect(self._check_owners)
            page.output_view.setProperty("sshGroupInput", True)
            page.group_input_banner.show()
            page.output_view.setToolTip(
                tr(
                    "SSH group keyboard input enabled. Never enter passwords or secrets in this group."
                )
            )
        with QSignalBlocker(self._action):
            self._action.setChecked(True)
        self._action.setText(tr("Group keyboard input ({p0}) · ON", p0=f"{len(pages)}"))
        self._window.statusBar().showMessage(
            tr(
                "Group keyboard input ON · {p0} selected SSH terminals. Disable before entering secrets.",
                p0=f"{len(pages)}",
            )
        )
        return True

    def disable(self, message=None):
        for page, backend in tuple(self._targets.items()):
            if isValid(page) and isValid(page.output_view):
                page.output_view.user_key_generated.disconnect(self._slots[page])
                page.group_input_stop_requested.disconnect(self._quick_stop)
                page.input_lock_changed.disconnect(self._check_owners)
                page.output_view.setProperty("sshGroupInput", False)
                page.group_input_banner.hide()
                page.output_view.setToolTip(self._tooltips[page])
            if isValid(backend):
                backend.finished.disconnect(self._check_owners)
                backend.destroyed.disconnect(self._check_owners)
        self._targets.clear()
        self._slots.clear()
        self._tooltips.clear()
        if isValid(self._action):
            with QSignalBlocker(self._action):
                self._action.setChecked(False)
            self._action.setText(tr("Group keyboard input…"))
        if message and isValid(self._window):
            self._window.statusBar().showMessage(message)

    def _quick_stop(self):
        self.disable(tr("Group keyboard input OFF."))

    @staticmethod
    def target_label(page):
        endpoint = page.backend.plan.endpoint
        username = page.backend.plan.profile.username
        return f"{sanitize_terminal_title(page.display_title)} · {sanitize_terminal_title(username)}@{endpoint.host}:{endpoint.port}"

    def _broadcast(self, source, intent):
        self._check_owners()
        if source not in self._targets or self._closed:
            return
        if (
            type(intent) is not tuple
            or len(intent) != 3
            or not isinstance(intent[0], int)
            or type(intent[1]) is not str
            or len(intent[1]) > MAX_GROUP_KEY_BYTES
            or not isinstance(intent[2], Qt.KeyboardModifier)
        ):
            self.disable(tr("Group input stopped: invalid keyboard event."))
            return
        key, text, modifiers = intent
        payloads = []
        for page in self._targets:
            if page is source:
                continue
            sequence = page.output_view._input_encoder.encode(
                key, text, modifiers, application_cursor_keys=page.screen.application_cursor_keys
            )
            if sequence is None:
                self.disable(
                    tr("Group input stopped: this key is not supported by a selected terminal.")
                )
                return
            try:
                data = sequence.encode(page.backend.text_encoding)
            except UnicodeError:
                self.disable(tr("Group input stopped: keyboard text could not be encoded."))
                return
            if len(data) > MAX_GROUP_KEY_BYTES:
                self.disable(tr("Group input stopped: key payload is too large."))
                return
            payloads.append((page, data))
        # The source's ordinary input path already sent its key. Replicate
        # only actual keyboard intent; never device replies, paste or mouse.
        for page, data in payloads:
            if page.backend is not self._targets.get(page) or not self.eligible(page):
                self.disable(tr("Group input stopped: selected SSH owner unavailable."))
                return
            page._send_direct_bytes(data)

    def configure(self, checked):
        if not checked:
            self.disable(tr("Group keyboard input OFF."))
            return
        self.disable()
        if self._closed:
            return
        pages = tuple(page for page in self._workspace._all_pages() if self.eligible(page))[
            :MAX_GROUP_TARGETS
        ]
        dialog = QDialog(self._window)
        dialog.setWindowTitle(tr("Choose SSH group keyboard targets"))
        layout = QVBoxLayout(dialog)
        note = WrappingLabel(
            tr(
                "Keyboard keys are sent to every checked SSH terminal. Never use this for passwords, sudo prompts or secrets. Remote secret prompts cannot be reliably detected. Paste, mouse, IME and automatic terminal replies stay local. New/reconnected tabs never join automatically."
            )
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        listing = QListWidget()
        for page in pages:
            item = QListWidgetItem(self.target_label(page), listing)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Enable selected terminals"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted and isValid(dialog)
            selected = tuple(
                page
                for i, page in enumerate(pages)
                if accepted and listing.item(i).checkState() == Qt.CheckState.Checked
            )
            if accepted and not self.enable(selected):
                self._window.statusBar().showMessage(
                    tr("Select 2-16 still-ready embedded SSH terminals. Group input remains OFF.")
                )
        finally:
            self._dialog = None
            if isValid(dialog):
                dialog.deleteLater()

    def stop(self):
        self._closed = True
        if self._dialog is not None and isValid(self._dialog):
            self._dialog.reject()
        self.disable()
