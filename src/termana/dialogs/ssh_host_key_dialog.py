"""Calm, branded confirmation dialog for a changed SSH server identity."""

from pathlib import Path
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.clipboard_text import copy_plain_text
from termana.widgets.wrapping_label import WrappingLabel

MINIMUM_DIALOG_WIDTH: Final = 620


class SshHostKeyDialog(QDialog):
    """Explain a changed host key without weakening the confirmation boundary."""

    def __init__(
        self,
        *,
        endpoint: str,
        received_key_type: str,
        received_fingerprint: str,
        stored_key_type: str,
        known_hosts_path: Path,
        line_number: int,
        parent: QWidget | None = None,
    ) -> None:
        """Build a compact confirmation with selectable technical data."""
        super().__init__(parent)

        self.setObjectName("sshHostKeyChangedDialog")
        self.setWindowTitle(tr("Server identity changed"))
        self.setModal(True)
        self.setMinimumWidth(MINIMUM_DIALOG_WIDTH)
        self.setSizeGripEnabled(False)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("sshHostKeyBodyScroll")
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.body_scroll.setMinimumHeight(96)
        self.body_scroll.setAutoFillBackground(False)
        self.body_scroll.viewport().setObjectName("sshHostKeyBodyViewport")
        self.body_scroll.viewport().setAutoFillBackground(False)

        self.body_widget = QWidget()
        self.body_widget.setObjectName("sshHostKeyBody")
        self.body_widget.setAutoFillBackground(False)
        self.body_widget.setMinimumWidth(0)
        self.body_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.body_layout = QVBoxLayout(self.body_widget)
        self.body_layout.setContentsMargins(28, 26, 28, 18)
        self.body_layout.setSpacing(18)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(16)
        header_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        warning_badge = QFrame()
        warning_badge.setObjectName("hostKeyWarningBadge")
        warning_badge.setFixedSize(48, 48)
        badge_layout = QVBoxLayout(warning_badge)
        badge_layout.setContentsMargins(0, 0, 0, 0)

        warning_symbol = QLabel("!")
        warning_symbol.setObjectName("hostKeyWarningSymbol")
        warning_symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        warning_symbol.setAccessibleName(tr("SSH identity notice"))
        scale_widget_font(warning_symbol, 2.0, weight=QFont.Weight.Bold)
        badge_layout.addWidget(warning_symbol)

        heading_layout = QVBoxLayout()
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(5)

        title = WrappingLabel(tr("Server identity changed"))
        title.setObjectName("hostKeyDialogTitle")
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scale_widget_font(title, 1.6, weight=QFont.Weight.Bold)

        self.endpoint_label = QLineEdit(endpoint)
        self.endpoint_label.setObjectName("sshHostEndpointLabel")
        self.endpoint_label.setReadOnly(True)
        self.endpoint_label.setAccessibleName(tr("SSH server address"))
        self.endpoint_label.setToolTip(plain_text_tooltip(endpoint))
        self.endpoint_label.setCursorPosition(0)
        self.endpoint_label.setMinimumWidth(0)
        self.endpoint_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )

        heading_layout.addWidget(title)
        heading_layout.addWidget(self.endpoint_label)
        header_layout.addWidget(warning_badge)
        header_layout.addLayout(heading_layout, 1)
        self.body_layout.addLayout(header_layout)

        explanation = WrappingLabel(
            tr(
                "Termana stopped the connection safely. This often happens after a server reinstall or a planned key rotation."
            )
        )
        explanation.setObjectName("hostKeyExplanation")
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        explanation.setWordWrap(True)
        explanation.setMinimumWidth(0)
        explanation.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.body_layout.addWidget(explanation)

        fingerprint_card = QFrame()
        fingerprint_card.setObjectName("hostKeyFingerprintCard")
        fingerprint_layout = QVBoxLayout(fingerprint_card)
        fingerprint_layout.setContentsMargins(16, 14, 16, 14)
        fingerprint_layout.setSpacing(8)

        fingerprint_heading_layout = QHBoxLayout()
        fingerprint_heading_layout.setContentsMargins(0, 0, 0, 0)
        fingerprint_heading_layout.setSpacing(10)

        fingerprint_title = WrappingLabel(
            tr("PRESENTED {p0} FINGERPRINT", p0=f"{received_key_type}")
        )
        fingerprint_title.setObjectName("hostKeyFingerprintTitle")
        fingerprint_title.setTextFormat(Qt.TextFormat.PlainText)
        fingerprint_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scale_widget_font(fingerprint_title, 0.85, weight=QFont.Weight.Bold)
        fingerprint_heading_layout.addWidget(fingerprint_title, 1)

        self.copy_button = QPushButton(tr("Copy"))
        self.copy_button.setObjectName("copyHostKeyFingerprintButton")
        self.copy_button.setAutoDefault(False)
        self.copy_button.setToolTip(tr("Copy fingerprint"))
        self.copy_button.setAccessibleName(tr("Copy SSH fingerprint"))
        self.copy_button.clicked.connect(self._copy_fingerprint)
        fingerprint_heading_layout.addWidget(self.copy_button)

        self.fingerprint_label = QLineEdit(received_fingerprint)
        self.fingerprint_label.setObjectName("sshHostFingerprintLabel")
        self.fingerprint_label.setReadOnly(True)
        self.fingerprint_label.setAccessibleName(
            tr("Presented {p0} SSH fingerprint", p0=f"{received_key_type}")
        )
        self.fingerprint_label.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.fingerprint_label.setCursorPosition(0)

        fingerprint_layout.addLayout(fingerprint_heading_layout)
        fingerprint_layout.addWidget(self.fingerprint_label)
        self.body_layout.addWidget(fingerprint_card)

        safety_hint = WrappingLabel(
            tr(
                "If you did not expect this change, cancel and verify the fingerprint with your hosting provider or server administrator."
            )
        )
        safety_hint.setObjectName("hostKeySafetyHint")
        safety_hint.setTextFormat(Qt.TextFormat.PlainText)
        safety_hint.setWordWrap(True)
        safety_hint.setMinimumWidth(0)
        safety_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.body_layout.addWidget(safety_hint)

        self.details_toggle = QToolButton()
        self.details_toggle.setObjectName("hostKeyDetailsToggle")
        self.details_toggle.setText(tr("Technical details"))
        self.details_toggle.setCheckable(True)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setAccessibleName(tr("Show SSH connection details"))
        self.details_toggle.toggled.connect(self._set_details_visible)
        self.body_layout.addWidget(
            self.details_toggle,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )

        self.details_panel = QFrame()
        self.details_panel.setObjectName("hostKeyDetailsCard")
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(14, 12, 14, 12)
        details_layout.setSpacing(6)

        path_title = WrappingLabel(tr("Saved host keys file"))
        path_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.known_hosts_path_input = QLineEdit(str(known_hosts_path))
        self.known_hosts_path_input.setObjectName("sshKnownHostsPath")
        self.known_hosts_path_input.setReadOnly(True)
        self.known_hosts_path_input.setAccessibleName(tr("Saved host keys file"))
        self.known_hosts_path_input.setToolTip(plain_text_tooltip(str(known_hosts_path)))
        self.known_hosts_path_input.setCursorPosition(0)
        self.known_hosts_path_input.setMinimumWidth(0)
        path_title.setBuddy(self.known_hosts_path_input)
        details_layout.addWidget(path_title)
        details_layout.addWidget(self.known_hosts_path_input)

        self.details_label = WrappingLabel(
            tr(
                "Saved key type: {p0}\nLine: {p1}\n\nAn unexpected key change can also mean the connection is being intercepted. Verify the fingerprint before reconnecting.",
                p0=f"{stored_key_type}",
                p1=f"{line_number}",
            )
        )
        self.details_label.setObjectName("sshHostKeyDetailsLabel")
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_label.setWordWrap(True)
        self.details_label.setMinimumWidth(0)
        self.details_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        details_layout.addWidget(self.details_label)
        self.details_panel.hide()
        self.body_layout.addWidget(self.details_panel)

        self.body_scroll.setWidget(self.body_widget)
        root_layout.addWidget(self.body_scroll, 1)

        self.footer_widget = QWidget()
        self.footer_widget.setObjectName("sshHostKeyFooter")
        footer_layout = QVBoxLayout(self.footer_widget)
        footer_layout.setContentsMargins(28, 14, 28, 24)
        footer_layout.setSpacing(12)

        footer_hint = WrappingLabel(
            tr(
                "Continue only if you have verified this fingerprint. Termana will remove the old record and verify exactly this key in the same tab. OpenSSH starts a fresh connection; your terminal history stays open."
            )
        )
        footer_hint.setObjectName("hostKeyFooterHint")
        footer_hint.setTextFormat(Qt.TextFormat.PlainText)
        footer_hint.setWordWrap(True)
        footer_hint.setMinimumWidth(0)
        footer_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        footer_layout.addWidget(footer_hint)

        self.button_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.button_layout.setContentsMargins(0, 2, 0, 0)
        self.button_layout.setSpacing(10)
        self.button_layout.addStretch()

        self.cancel_button = QPushButton(tr("Keep saved key"))
        self.cancel_button.setObjectName("keepSshHostKeyButton")
        self.cancel_button.setAccessibleName(tr("Keep the currently saved SSH key"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)

        self.replace_button = QPushButton(tr("Replace key and continue"))
        self.replace_button.setObjectName("replaceSshHostKeyButton")
        self.replace_button.setAccessibleName(
            tr("Replace the saved SSH key with the reviewed fingerprint and continue in this tab")
        )
        self.replace_button.setAutoDefault(False)
        self.replace_button.setDefault(False)
        self.replace_button.clicked.connect(self.accept)

        self.button_layout.addWidget(self.cancel_button)
        self.button_layout.addWidget(self.replace_button)
        footer_layout.addLayout(self.button_layout)
        root_layout.addWidget(self.footer_widget)

        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Stack footer actions when large text leaves too little horizontal room."""
        super().resizeEvent(event)

        footer_margins = self.footer_widget.layout().contentsMargins()
        available_width = max(
            0,
            self.footer_widget.width() - footer_margins.left() - footer_margins.right(),
        )
        required_width = (
            self.cancel_button.sizeHint().width()
            + self.replace_button.sizeHint().width()
            + self.button_layout.spacing()
        )
        direction = (
            QBoxLayout.Direction.TopToBottom
            if required_width > available_width
            else QBoxLayout.Direction.LeftToRight
        )
        if self.button_layout.direction() != direction:
            self.button_layout.setDirection(direction)
            self.footer_widget.updateGeometry()

    def _copy_fingerprint(self) -> None:
        """Copy the exact displayed fingerprint without altering the dialog result."""
        copied = copy_plain_text(self.fingerprint_label.text())
        self.copy_button.setText(tr("Copied") if copied else tr("Copy unavailable"))
        self.copy_button.setAccessibleName(
            tr("Fingerprint copied to clipboard")
            if copied
            else tr("Clipboard unavailable; fingerprint not copied")
        )

    def _set_details_visible(self, visible: bool) -> None:
        """Expand technical information only when the user asks for it."""
        self.details_panel.setVisible(visible)
        arrow = Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        self.details_toggle.setArrowType(arrow)
        self.body_layout.activate()
        self.body_widget.adjustSize()
        if visible:
            self.body_scroll.ensureWidgetVisible(self.details_panel, 0, 12)
