"""Bounded, plain-text review before a gateway configuration is saved."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout

from termana.i18n import tr
from termana.widgets.wrapping_label import WrappingLabel


class SshGatewayImportDialog(QDialog):
    def __init__(self, gateway, *, replacing=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Review gateway settings"))
        self.setObjectName("sshGatewayImportDialog")
        self.setModal(True)
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)
        title = WrappingLabel(
            tr(
                "Replace this gateway in Termana?"
                if replacing
                else "Import this gateway into Termana?"
            )
        )
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        details = [
            f"Alias: {gateway.alias}",
            f"Host: {gateway.host}",
            f"Port: {gateway.port}",
            f"Login: {gateway.username or 'Local username'}",
            *[f"Key reference (not copied): {value}" for value in gateway.identity_files],
            f"Identities only: {'Default' if gateway.identities_only is None else 'Yes' if gateway.identities_only else 'No'}",
            f"Agent: {gateway.identity_agent or 'OpenSSH default'}",
            f"Connect timeout: {gateway.connect_timeout if gateway.connect_timeout is not None else 'Default'}",
            f"Keepalive interval: {gateway.server_alive_interval if gateway.server_alive_interval is not None else 'Default'}",
            f"Keepalive count: {gateway.server_alive_count_max if gateway.server_alive_count_max is not None else 'Default'}",
        ]
        self.details = QPlainTextEdit("\n".join(details))
        self.details.setReadOnly(True)
        self.details.setTabChangesFocus(True)
        self.details.setAccessibleName(tr("Gateway settings to import"))
        layout.addWidget(self.details, 1)
        self.notice = WrappingLabel(
            tr(
                "User/system SSH configs are not inherited. Key files stay where they are. "
                "No credentials or fingerprints are imported. Changes apply to new connections only."
            )
        )
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setProperty("actionRole", "secondary")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.import_button = QPushButton(tr("Import"))
        self.import_button.setProperty("actionRole", "primary")
        self.import_button.setAutoDefault(False)
        self.import_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.import_button)
        layout.addLayout(buttons)
        self.cancel_button.setFocus()
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        self.resize(
            min(620, available.width() - 40) if available else 620,
            min(460, available.height() - 80) if available else 460,
        )
