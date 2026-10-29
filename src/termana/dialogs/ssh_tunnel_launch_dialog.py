"""One-launch consent for local rules and inherited OpenSSH forwarding settings."""

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.models.ssh_jump import SshJumpMode
from termana.qt_lifecycle import schedule_delete_later
from termana.terminal.ssh_command import SSH_CONFIGURED_FORWARDS_NOTICE
from termana.ui_typography import scale_widget_font
from termana.widgets.wrapping_label import WrappingLabel


def _endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class SshTunnelLaunchDialog(QDialog):
    """Keep exact rules copyable and the safe default visible even with 16 rules."""

    def __init__(self, *, profile: ConnectionProfile, parent: QWidget | None = None,
                 inherited_config: bool = True) -> None:
        if (
            type(profile) is not ConnectionProfile
            or profile.protocol is not ConnectionProtocol.SSH
            or profile.ssh_tunnels.is_default
            or profile.username is None
        ):
            raise ValueError("Tunnel confirmation needs an SSH profile with a login and rules")
        super().__init__(parent)
        self.setObjectName("sshTunnelLaunchDialog")
        self.setProperty("dialogRole", "actionNotice")
        self.setWindowTitle(tr("Connect with SSH tunnels?"))
        self.setAccessibleName(tr("Confirm SSH tunnel launch"))
        self.setModal(True)
        self.setSizeGripEnabled(True)
        self.setMinimumWidth(480)
        self.resize(640, 600)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.body_scroll = QScrollArea()
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setMinimumHeight(80)
        self.body = QWidget()
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)
        title = self._label(tr("Connect with SSH tunnels?"), "actionDialogTitle")
        scale_widget_font(title, 1.4, weight=QFont.Weight.Bold)
        layout.addWidget(title)
        self.endpoint_display = QPlainTextEdit(
            f"{profile.username}@{_endpoint(profile.host, profile.port)}"
        )
        self.endpoint_display.setReadOnly(True)
        self.endpoint_display.setTabChangesFocus(True)
        self.endpoint_display.setAccessibleName(tr("SSH destination and login"))
        self.endpoint_display.setMaximumHeight(86)
        layout.addWidget(self.endpoint_display)
        route = profile.ssh_jump
        route_text = (
            tr("Jump host: {p0}", p0=f"{route.proxy_jump_value}")
            if route.mode is SshJumpMode.JUMP
            else tr("Route: direct connection")
            if route.mode is SshJumpMode.DIRECT
            else tr("Route: SSH config")
        )
        self.rules_display = QPlainTextEdit(
            route_text
            + "\n\n"
            + "\n".join(
                f"127.0.0.1:{rule.local_port} → "
                f"{_endpoint(rule.destination_host, rule.destination_port)}"
                for rule in profile.ssh_tunnels.local_forwards
            )
        )
        self.rules_display.setReadOnly(True)
        self.rules_display.setTabChangesFocus(True)
        self.rules_display.setAccessibleName(
            tr("Local listeners and destinations reached from SSH server")
        )
        self.rules_display.setMinimumHeight(120)
        self.rules_display.setMaximumHeight(210)
        layout.addWidget(self.rules_display)
        notice = (SSH_CONFIGURED_FORWARDS_NOTICE if inherited_config else
                  "Only these Termana local rules will open listeners, restricted to 127.0.0.1. "
                  "User/system SSH configs and their forwarding settings are not inherited. "
                  "Gateway imports do not include forwarding rules.")
        self.notice = self._label(tr(notice), "actionDialogBody")
        layout.addWidget(self.notice)
        layout.addWidget(
            self._label(
                tr(
                    "The destination service is reached from the final SSH server. Other local programs can use these ports. A busy local port stops this SSH attempt; a connected SSH session does not prove the forwarded service is reachable. This choice applies only to this connection and is not saved."
                ),
                "mutedText",
            )
        )
        self.body_scroll.setWidget(self.body)
        root.addWidget(self.body_scroll, 1)
        self.footer = QWidget()
        buttons = QHBoxLayout(self.footer)
        buttons.setContentsMargins(24, 12, 24, 18)
        buttons.addStretch()
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setProperty("actionRole", "secondary")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.connect_button = QPushButton(tr("Connect"))
        self.connect_button.setAccessibleName(tr("Connect with these SSH tunnels"))
        self.connect_button.setProperty("actionRole", "primary")
        self.connect_button.setAutoDefault(False)
        self.connect_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.connect_button)
        root.addWidget(self.footer)
        self.cancel_button.setFocus()
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(640, available.width()), min(600, max(240, available.height() - 80)))

    @staticmethod
    def _label(text: str, name: str) -> QLabel:
        label = WrappingLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label


def confirm_ssh_tunnel_launch(
    *,
    profile: ConnectionProfile,
    parent: QWidget | None,
    dialog_created: Callable[[SshTunnelLaunchDialog], None] | None = None,
    inherited_config: bool = True,
) -> bool:
    dialog = SshTunnelLaunchDialog(profile=profile, parent=parent, inherited_config=inherited_config)
    try:
        if dialog_created is not None:
            dialog_created(dialog)
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        schedule_delete_later(dialog)
