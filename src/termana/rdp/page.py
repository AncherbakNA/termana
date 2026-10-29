"""A real graphical session page, deliberately not a terminal backend/page."""

from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from termana.dialogs.action_dialog import ActionDialog
from termana.i18n import tr
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.rdp.certificate import display_fingerprint
from termana.rdp.clipboard import RdpClipboardBridge
from termana.rdp.controller import RdpController
from termana.rdp.endpoint import bounded_desktop_size, validate_display_size
from termana.rdp.errors import end_explanation
from termana.rdp.fields import RdpIdentityLineEdit
from termana.rdp.identity_services import RdpIdentityServices
from termana.rdp.surface import RdpSurface
from termana.rdp.tools import RdpSessionTools
from termana.rdp.view import RdpDesktopView
from termana.widgets.wrapping_label import WrappingLabel


class RdpPage(QWidget):
    ready_to_delete = Signal()
    state_changed = Signal()
    duplicate_requested = Signal()

    def __init__(self, host, port=3389, parent=None, *, controller_factory=RdpController,
                 display_size=(1280, 720), clipboard=None, vault=None, trust_store=None,
                 identity_repository=None):
        super().__init__(parent)
        self.setObjectName("rdpSessionPage")
        self.session_id = uuid4()
        self.terminal_name = f"RDP · {host}" if port == 3389 else f"RDP · {host}:{port}"
        self.display_title = self.terminal_name
        self._shutdown = False
        self._request = None
        self._focus_guard = lambda: True
        self.profile = None
        self._other_user_pending = False
        self._confirmation = None
        self._start_fullscreen = False
        self._last_resize = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        bar = QFrame(self)
        bar.setObjectName("rdpSessionBar")
        header = QHBoxLayout(bar)
        header.setContentsMargins(10, 6, 10, 6)
        self.status = WrappingLabel(tr("Connecting"), bar)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        header.addWidget(self.status, 1)
        self.reconnect = QPushButton(tr("Reconnect"), bar)
        self.disconnect = QPushButton(tr("Disconnect"), bar)
        for button in (self.reconnect, self.disconnect):
            button.setProperty("actionRole", "secondary")
            header.addWidget(button)
        self.reconnect.setEnabled(False)
        self.reconnect.hide()  # Only meaningful after the current attempt ends.
        root.addWidget(bar)
        self.connection_notice = QLabel(self)
        self.connection_notice.setObjectName("rdpConnectionError")
        self.connection_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.connection_notice.setWordWrap(True)
        self.connection_notice.setContentsMargins(10, 0, 10, 8)
        self.connection_notice.hide()
        root.addWidget(self.connection_notice)
        self.stack = QStackedWidget(self)
        root.addWidget(self.stack, 1)
        self.surface = RdpSurface(self.session_id, self)
        self.desktop_view = RdpDesktopView(self.surface, self)
        self.stack.addWidget(self.desktop_view)
        self.trust = QWidget(self)
        trust_layout = self._prompt_layout(self.trust)
        self.trust_layout = trust_layout
        trust_layout.setContentsMargins(24, 20, 24, 20)
        trust_layout.addStretch()
        self.certificate_text = QLabel(self.trust)
        self.certificate_text.setWordWrap(True)
        self.certificate_text.setTextFormat(Qt.TextFormat.PlainText)
        trust_layout.addWidget(self.certificate_text)
        trust_hint = QLabel(tr("Verify this fingerprint with the server owner before continuing."), self.trust)
        trust_hint.setWordWrap(True)
        trust_layout.addWidget(trust_hint)
        trust_actions = QHBoxLayout()
        self.trust_once = QPushButton(tr("Connect once"), self.trust)
        self.trust_cancel = QPushButton(tr("Cancel"), self.trust)
        self.trust_once.setProperty("actionRole", "primary")
        self.trust_cancel.setProperty("actionRole", "secondary")
        trust_actions.addStretch()
        trust_actions.addWidget(self.trust_cancel)
        trust_actions.addWidget(self.trust_once)
        trust_layout.addLayout(trust_actions)
        trust_layout.addStretch()
        self.stack.addWidget(self.trust)
        self.login = QWidget(self)
        form_root = self._prompt_layout(self.login)
        self.login_layout = form_root
        form_root.setContentsMargins(24, 20, 24, 20)
        form_root.addStretch()
        hint = QLabel(tr("Sign in to Windows. Use Remember only if you want to save this sign-in."), self.login)
        hint.setWordWrap(True)
        form_root.addWidget(hint)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.username = RdpIdentityLineEdit(self.login)
        self.domain = RdpIdentityLineEdit(self.login)
        self.password = RdpIdentityLineEdit(self.login)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setInputMethodHints(Qt.InputMethodHint.ImhSensitiveData
                                         | Qt.InputMethodHint.ImhNoPredictiveText)
        for name, edit in (("Username", self.username), ("Domain (optional)", self.domain),
                           ("Password", self.password)):
            edit.setMaxLength(512 if edit is not self.password else 4096)
            edit.setAccessibleName(tr(name))
            form.addRow(tr(name), edit)
        form_root.addLayout(form)
        self.login_error = QLabel(self.login)
        form_root.addWidget(self.login_error)
        login_actions = QHBoxLayout()
        self.sign_in = QPushButton(tr("Sign in"), self.login)
        self.login_cancel = QPushButton(tr("Cancel"), self.login)
        self.sign_in.setProperty("actionRole", "primary")
        self.login_cancel.setProperty("actionRole", "secondary")
        login_actions.addStretch()
        login_actions.addWidget(self.login_cancel)
        login_actions.addWidget(self.sign_in)
        form_root.addLayout(login_actions)
        form_root.addStretch()
        self.stack.addWidget(self.login)
        self.controller = controller_factory(self.surface, host, port, self)
        self.controller.display_size = validate_display_size(display_size)
        self.controller.state_changed.connect(self._state)
        self.controller.certificate_requested.connect(self._certificate)
        self.controller.credentials_requested.connect(self._credentials)
        self.controller.stopped.connect(self._stopped)
        self.reconnect.clicked.connect(self.start)
        self.disconnect.clicked.connect(self.controller.stop)
        self.trust_cancel.clicked.connect(self.controller.stop)
        self.login_cancel.clicked.connect(self.controller.stop)
        self.trust_once.clicked.connect(self._accept_certificate)
        self.sign_in.clicked.connect(self._sign_in)
        self.password.returnPressed.connect(self._sign_in)
        self.tools = RdpSessionTools(self)
        root.insertWidget(1, self.tools.toolbar)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(250)
        self._resize_timer.timeout.connect(self._resize_remote)
        self.desktop_view.viewport_resized.connect(self._schedule_resize)
        self.controller.channels_changed.connect(self._schedule_resize)
        self.surface.eligibility_changed.connect(self._schedule_resize)
        self.clipboard_bridge = RdpClipboardBridge(self, clipboard=clipboard)
        self.identity_services = RdpIdentityServices(self, vault=vault, trust_store=trust_store,
                                                     repository=identity_repository)

    @staticmethod
    def _prompt_layout(widget):
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        return QVBoxLayout(body)

    @property
    def session_ended(self):
        return not self.controller.running

    def apply_profile(self, profile):
        """Snapshot public preferences before starting; edits never mutate a live session."""
        if (type(profile) is not ConnectionProfile or profile.protocol is not ConnectionProtocol.RDP
                or self.controller.running
                or (profile.host, profile.port) != (self.controller.host, self.controller.port)):
            raise ValueError("RDP profile does not match this inactive session")
        self.profile = profile
        self.terminal_name = self.display_title = profile.name
        options = profile.rdp_options
        self.controller.options = options
        self.controller.display_size = (options.width, options.height)
        self.username.setText(options.username)
        self.domain.setText(options.domain)
        self.tools.set_view_only(options.view_only)
        zoom = None if options.dynamic_resolution else options.zoom
        self.desktop_view.set_zoom(zoom)
        self.tools.zoom_actions[zoom].setChecked(True)
        for value, action in self.tools.zoom_actions.items():
            action.setEnabled(not options.dynamic_resolution or value is None)
        self.tools.set_keyboard_mode(options.keyboard_mode)
        self._start_fullscreen = options.full_screen

    def set_focus_guard(self, guard):
        self._focus_guard = guard

    @property
    def requires_close_confirmation(self):
        return self.controller.running

    def start(self):
        if self._shutdown:
            return
        self.identity_services.start()
        self.password.clear()
        self._last_resize = None
        self._resize_timer.stop()
        self.tools.exit_fullscreen()
        self.stack.setCurrentWidget(self.desktop_view)
        self.reconnect.setEnabled(False)
        self.reconnect.hide()
        self.disconnect.setEnabled(True)
        self.disconnect.show()
        self.controller.start()

    def _schedule_resize(self, *_):
        if self._dynamic_eligible():
            self._resize_timer.start()
        else:
            self._resize_timer.stop()

    def synchronize_workspace_geometry(self):
        """Re-evaluate after a pane reparent, even without another Qt focus/resize."""
        self._schedule_resize()

    def _dynamic_eligible(self):
        return (not self._shutdown and self._focus_guard()
                and self.controller.options.dynamic_resolution
                and self.controller.state == "Connected"
                and self.controller.channel_capabilities & 2
                and self.desktop_view.isVisible() and self.surface.isActiveWindow())

    def _resize_remote(self):
        if not self._dynamic_eligible():
            return
        viewport = self.desktop_view.viewport().size()
        size = bounded_desktop_size(viewport.width(), viewport.height())
        if size != self._last_resize and self.controller.resize_remote(*size):
            self._last_resize = size

    def _certificate(self, request):
        self._request, fingerprint, flags = request
        chunks = display_fingerprint(fingerprint)
        address = f"{self.controller.host}:{self.controller.port}"
        address = "\n".join(address[i:i+24] for i in range(0,len(address),24))
        self.certificate_text.setText(tr("Certificate could not be verified automatically.")
                                      + f"\n{address}\nSHA256:\n{chunks}\n"
                                      + tr("Only Remember saves a certificate exception.")
                                      + ("\n" + tr("The certificate name differs from this address.")
                                         if flags & 128 else ""))
        self.stack.setCurrentWidget(self.trust)
        if self._focus_guard() and self.isActiveWindow() and self.isVisible():
            self.trust_cancel.setFocus()
        self.identity_services.certificate()

    def _accept_certificate(self):
        if self.identity_services.accept_certificate():
            self.stack.setCurrentWidget(self.desktop_view)

    def _credentials(self, request):
        self._request = request
        self.password.clear()
        self.login_error.clear()
        self.stack.setCurrentWidget(self.login)
        self.identity_services.credentials()
        if self._focus_guard() and self.isActiveWindow() and self.isVisible():
            (self.password if self.username.text() else self.username).setFocus()

    def _sign_in(self):
        secret = self.password.text()
        try:
            if not self.controller.submit_credentials(self._request, self.username.text().strip(),
                                                       self.domain.text().strip(), secret):
                self.login_error.setText(tr("Enter a username and password."))
                return
            self.password.clear()
            self.identity_services.submitted(self.username.text().strip(), self.domain.text().strip(), secret)
            self.stack.setCurrentWidget(self.desktop_view)
        finally:
            secret = ""

    def _state(self, state):
        if hasattr(self, "identity_services"):
            self.identity_services.state(state)
        self.status.setText(tr(state))
        self.connection_notice.clear()
        self.connection_notice.hide()
        if state.startswith("Session ended") and self.controller.end_code is not None:
            self.connection_notice.setText(tr(end_explanation(self.controller.end_code)))
            self.connection_notice.show()
        elif state=="Failed":
            self.connection_notice.setText(tr("The RDP connection could not complete. Check the server and connection settings."))
            self.connection_notice.show()
        if state in {"Failed", "Disconnecting"} or state.startswith("Session ended"):
            self.password.clear()
            self.tools.exit_fullscreen()
            self.stack.setCurrentWidget(self.desktop_view)
        if (state == "Connected" and self.isVisible() and self.isActiveWindow()
                and self._focus_guard()):
            self.surface.setFocus(Qt.FocusReason.OtherFocusReason)
            if self._start_fullscreen:
                owner = self.controller.owner
                def full():
                    if (owner == self.controller.owner and self.controller.state == "Connected"
                            and not self._shutdown and self.isVisible() and self.isActiveWindow()
                            and self._focus_guard()):
                        self.tools.enter_fullscreen()
                QTimer.singleShot(0, self, full)
        self.state_changed.emit()
        if hasattr(self, "_resize_timer"):
            self._schedule_resize()

    def _stopped(self):
        self.password.clear()
        self.tools.exit_fullscreen()
        self.stack.setCurrentWidget(self.desktop_view)
        self.reconnect.setEnabled(not self._shutdown)
        self.reconnect.setVisible(not self._shutdown)
        self.disconnect.setEnabled(False)
        self.disconnect.hide()
        self.state_changed.emit()
        if self._shutdown:
            self.ready_to_delete.emit()
        elif self._other_user_pending:
            self._other_user_pending = False
            QTimer.singleShot(0, self, self.start)

    def reconnect_as_other_user(self):
        if (self._shutdown or self._other_user_pending or not self._focus_guard()
                or not self.isVisible() or QApplication.activeModalWidget() is not None
                or (self.controller.running and self.controller.state != "Connected")):
            return
        owner, was_running = self.controller.owner, self.controller.running
        if was_running and not self._confirm_other_user():
            return
        # The confirmation runs a nested event loop. Never apply its old consent
        # to a replacement attempt, hidden tab, or a session which already ended.
        if (self._shutdown or not self._focus_guard() or not self.isVisible()
                or self.controller.owner != owner
                or (was_running and self.controller.state != "Connected")):
            return
        self.username.clear()
        self.domain.clear()
        self.password.clear()
        self.identity_services.other_user()
        if self.controller.running:
            self._other_user_pending = True
            self.controller.stop()
        else:
            self.start()

    def _confirm_other_user(self):
        dialog = ActionDialog(
            object_name="rdpOtherUserConfirmation", window_title=tr("Reconnect with another user?"),
            title=tr("Reconnect with another user?"), subtitle=tr("The saved profile stays unchanged"),
            body=tr("Disconnect this desktop and start a new sign-in in the same tab. Windows will not be signed out."),
            primary_text=tr("Reconnect"), primary_object_name="rdpConfirmOtherUserButton",
            secondary_text=tr("Cancel"), secondary_object_name="rdpCancelOtherUserButton",
            parent=self.window(),
        )
        self._confirmation = dialog
        try:
            return dialog.exec() == dialog.DialogCode.Accepted
        finally:
            self._confirmation = None
            dialog.deleteLater()

    def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self._other_user_pending = False
        if self._confirmation is not None:
            self._confirmation.reject()
        self.password.clear()
        self.identity_services.shutdown()
        self.tools.shutdown()
        self._resize_timer.stop()
        self.clipboard_bridge.shutdown()
        if self.controller.running:
            self.controller.stop()
        else:
            self.ready_to_delete.emit()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "tools") and not self._focus_guard():
            self.tools.exit_fullscreen()
