"""Calm, reusable confirmation for starting an unencrypted Telnet session."""

from __future__ import annotations

import html
import unicodedata
from collections.abc import Callable
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.connection import (
    MAX_CONNECTION_HOST_CHARACTERS,
    MAX_NETWORK_PORT,
    MIN_NETWORK_PORT,
)
from termana.qt_lifecycle import schedule_delete_later
from termana.storage.telnet_warning_store import TelnetWarningStore, TelnetWarningStoreError
from termana.ui_typography import scale_widget_font
from termana.widgets.wrapping_label import WrappingLabel

MINIMUM_TELNET_DIALOG_WIDTH: Final = 560
DEFAULT_TELNET_DIALOG_WIDTH: Final = 640
DEFAULT_TELNET_DIALOG_HEIGHT: Final = 400
TELNET_NOTICE: Final = (
    "Telnet sends session traffic without encryption. Someone who can observe "
    "this network may be able to read usernames, passwords, commands, and output."
)
TELNET_SAFETY_HINT: Final = (
    "Use Telnet only for equipment or networks you trust. Termana does not send "
    "saved credentials automatically."
)
_DIRECTIONAL_FORMATS: Final = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)


def _validate_endpoint(host: str, port: int) -> None:
    """Defend the public UI boundary while accepting normal validated hosts."""
    if not isinstance(host, str) or not host:
        raise ValueError("Telnet host must be a non-empty string")
    if host != host.strip() or any(character.isspace() for character in host):
        raise ValueError("Telnet host cannot contain whitespace")
    if len(host) > MAX_CONNECTION_HOST_CHARACTERS:
        raise ValueError("Telnet host is too long")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        or unicodedata.bidirectional(character) in _DIRECTIONAL_FORMATS
        for character in host
    ):
        raise ValueError("Telnet host contains unsafe control characters")
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError("Telnet port must be an integer")
    if not MIN_NETWORK_PORT <= port <= MAX_NETWORK_PORT:
        raise ValueError(f"Telnet port must be between {MIN_NETWORK_PORT} and {MAX_NETWORK_PORT}")


def _format_endpoint(host: str, port: int) -> str:
    """Keep IPv6 endpoints unambiguous without altering the validated host."""
    display_host = host
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        display_host = f"[{host}]"
    return f"{display_host}:{port}"


class TelnetWarningDialog(QDialog):
    """Present one explicit Telnet choice with Cancel as the safe default."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        parent: QWidget | None = None,
        remember_endpoint: Callable[[], None] | None = None,
    ) -> None:
        """Build a bounded, plain-text confirmation for one validated endpoint."""
        _validate_endpoint(host, port)
        super().__init__(parent)

        self.endpoint = _format_endpoint(host, port)
        self._remember_endpoint = remember_endpoint
        self.setObjectName("telnetConnectionWarningDialog")
        self.setProperty("dialogRole", "actionNotice")
        self.setWindowTitle(tr("Connect using Telnet?"))
        self.setAccessibleName(tr("Confirm unencrypted Telnet connection"))
        self.setAccessibleDescription(
            tr("Choose whether to connect to {p0} without encryption.", p0=f"{self.endpoint}")
        )
        self.setModal(True)
        self.setSizeGripEnabled(True)

        screen = self.screen()
        available_size = screen.availableGeometry().size() if screen is not None else None
        available_width = (
            available_size.width() if available_size is not None else DEFAULT_TELNET_DIALOG_WIDTH
        )
        available_height = (
            available_size.height() if available_size is not None else DEFAULT_TELNET_DIALOG_HEIGHT
        )
        self.setMinimumWidth(min(MINIMUM_TELNET_DIALOG_WIDTH, available_width))
        self.setMaximumSize(available_width, available_height)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("telnetWarningBodyScroll")
        self.body_scroll.setAccessibleName(tr("Telnet connection notice"))
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setMinimumHeight(80)
        self.body_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.body_scroll.setAutoFillBackground(False)
        self.body_scroll.viewport().setObjectName("telnetWarningBodyViewport")
        self.body_scroll.viewport().setAutoFillBackground(False)

        self.body_widget = QWidget()
        self.body_widget.setObjectName("telnetWarningBody")
        self.body_widget.setMinimumWidth(0)
        self.body_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        body_layout = QVBoxLayout(self.body_widget)
        body_layout.setContentsMargins(28, 26, 28, 20)
        body_layout.setSpacing(18)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(16)
        header_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        badge = QFrame()
        badge.setObjectName("actionDialogBadge")
        badge.setProperty("tone", "notice")
        badge.setFixedSize(46, 46)
        badge_layout = QVBoxLayout(badge)
        badge_layout.setContentsMargins(0, 0, 0, 0)

        badge_symbol = QLabel(tr("i"))
        badge_symbol.setObjectName("actionDialogBadgeSymbol")
        badge_symbol.setTextFormat(Qt.TextFormat.PlainText)
        badge_symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge_symbol.setAccessibleName(tr("Telnet security notice"))
        scale_widget_font(badge_symbol, 1.45, weight=QFont.Weight.Bold)
        badge_layout.addWidget(badge_symbol)

        heading_layout = QVBoxLayout()
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(5)

        self.title_label = self._plain_wrapping_label(
            tr("Connect without encryption?"),
            "actionDialogTitle",
        )
        self.title_label.setAccessibleName(tr("Telnet connection confirmation"))
        scale_widget_font(self.title_label, 1.5, weight=QFont.Weight.Bold)

        self.subtitle_label = self._plain_wrapping_label(
            tr("Review the destination before starting this Telnet session."),
            "actionDialogSubtitle",
        )
        heading_layout.addWidget(self.title_label)
        heading_layout.addWidget(self.subtitle_label)
        header_layout.addWidget(badge)
        header_layout.addLayout(heading_layout, 1)
        body_layout.addLayout(header_layout)

        self.notice_label = self._plain_wrapping_label(tr(TELNET_NOTICE), "actionDialogBody")
        self.notice_label.setAccessibleName(tr("Telnet encryption notice"))
        body_layout.addWidget(self.notice_label)

        endpoint_card = QFrame()
        endpoint_card.setObjectName("telnetEndpointCard")
        endpoint_layout = QVBoxLayout(endpoint_card)
        endpoint_layout.setContentsMargins(16, 13, 16, 14)
        endpoint_layout.setSpacing(7)

        endpoint_heading = QLabel(tr("DESTINATION"))
        endpoint_heading.setObjectName("telnetEndpointHeading")
        endpoint_heading.setTextFormat(Qt.TextFormat.PlainText)
        endpoint_heading.setAccessibleName(tr("Telnet destination"))
        scale_widget_font(endpoint_heading, 0.85, weight=QFont.Weight.Bold)

        self.endpoint_field = QLineEdit(self.endpoint)
        self.endpoint_field.setObjectName("telnetEndpointField")
        self.endpoint_field.setReadOnly(True)
        self.endpoint_field.setMinimumWidth(0)
        self.endpoint_field.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self.endpoint_field.setToolTip(
            tr("<span>Full Telnet endpoint: {p0}</span>", p0=f"{html.escape(self.endpoint)}")
        )
        self.endpoint_field.setAccessibleName(tr("Telnet endpoint"))
        self.endpoint_field.setAccessibleDescription(
            tr(
                "Full Telnet endpoint: {p0}. Read-only and selectable for copying.",
                p0=f"{self.endpoint}",
            )
        )
        self.endpoint_field.setCursorPosition(0)

        endpoint_layout.addWidget(endpoint_heading)
        endpoint_layout.addWidget(self.endpoint_field)
        body_layout.addWidget(endpoint_card)

        self.safety_label = self._plain_wrapping_label(
            TELNET_SAFETY_HINT,
            "telnetSafetyHint",
        )
        self.safety_label.setAccessibleName(tr("Telnet usage guidance"))
        body_layout.addWidget(self.safety_label)
        self.remember_checkbox = QCheckBox(tr("Don't warn again for this connection"))
        self.remember_checkbox.setObjectName("rememberTelnetWarning")
        self.remember_checkbox.setToolTip(tr("Only this Telnet host and port. You can reset this in Settings."))
        body_layout.addWidget(self.remember_checkbox)
        self.remember_error = self._plain_wrapping_label("", "telnetRememberError")
        self.remember_error.hide()
        body_layout.addWidget(self.remember_error)
        body_layout.addStretch()

        self.body_scroll.setWidget(self.body_widget)
        root_layout.addWidget(self.body_scroll, 1)

        self.footer_widget = QWidget()
        self.footer_widget.setObjectName("telnetWarningFooter")
        footer_layout = QVBoxLayout(self.footer_widget)
        footer_layout.setContentsMargins(28, 14, 28, 22)
        footer_layout.setSpacing(10)

        self.button_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.button_layout.setContentsMargins(0, 0, 0, 0)
        self.button_layout.setSpacing(10)
        self.button_layout.addStretch()

        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setObjectName("cancelTelnetButton")
        self.cancel_button.setProperty("actionRole", "secondary")
        self.cancel_button.setAccessibleName(tr("Cancel Telnet connection"))
        self.cancel_button.setAccessibleDescription(
            tr("Close this notice without starting a Telnet connection.")
        )
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)

        self.connect_button = QPushButton(tr("Connect with Telnet"))
        self.connect_button.setObjectName("connectTelnetButton")
        self.connect_button.setProperty("actionRole", "primary")
        self.connect_button.setAccessibleName(tr("Connect with Telnet"))
        self.connect_button.setAccessibleDescription(
            tr("Start an unencrypted Telnet session with {p0}.", p0=f"{self.endpoint}")
        )
        # Cancel owns the initial default, while an explicit keyboard focus move
        # to Connect should still make Return behave like a normal push button.
        self.connect_button.setAutoDefault(True)
        self.connect_button.setDefault(False)
        self.connect_button.clicked.connect(self.accept)

        self.button_layout.addWidget(self.cancel_button)
        self.button_layout.addWidget(self.connect_button)
        footer_layout.addLayout(self.button_layout)
        root_layout.addWidget(self.footer_widget)

        target_width = min(DEFAULT_TELNET_DIALOG_WIDTH, available_width)
        self._update_button_layout(target_width)
        self.resize(
            target_width,
            min(DEFAULT_TELNET_DIALOG_HEIGHT, available_height),
        )
        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def accept(self) -> None:
        if self.remember_checkbox.isChecked() and self._remember_endpoint is not None:
            try:
                self._remember_endpoint()
            except TelnetWarningStoreError:
                self.remember_error.setText(tr("Could not save this choice. Uncheck the box to connect once."))
                self.remember_error.show()
                return
        super().accept()

    @staticmethod
    def _plain_wrapping_label(text: str, object_name: str) -> QLabel:
        """Create a bounded label that cannot interpret endpoint-like markup."""
        label = WrappingLabel(text)
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep both actions reachable by stacking them when text needs more room."""
        super().resizeEvent(event)
        self._update_button_layout(self.width())

    def _update_button_layout(self, dialog_width: int) -> None:
        """Choose a footer direction without allowing buttons to widen the dialog."""
        footer_layout = self.footer_widget.layout()
        if footer_layout is None:
            return
        margins = footer_layout.contentsMargins()
        available_width = max(0, dialog_width - margins.left() - margins.right())
        label = tr("Don't warn again for this connection")
        if self.remember_checkbox.fontMetrics().horizontalAdvance(label) + 36 > available_width:
            label = tr("Don't warn again\nfor this connection")
        self.remember_checkbox.setText(label)
        required_width = (
            self.cancel_button.sizeHint().width()
            + self.connect_button.sizeHint().width()
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


def confirm_telnet_connection(
    *,
    host: str,
    port: int,
    parent: QWidget | None = None,
    consent_store: TelnetWarningStore | None = None,
) -> bool:
    """Authorize Telnet by a fresh click or an explicitly remembered endpoint choice."""
    _validate_endpoint(host, port)
    read_failed = False
    if consent_store is not None:
        try:
            if consent_store.is_remembered(host, port):
                return True
        except TelnetWarningStoreError:
            read_failed = True  # Fail closed to a fresh explicit warning.
    dialog = TelnetWarningDialog(host=host, port=port, parent=parent,
        remember_endpoint=(lambda: consent_store.remember(host, port)) if consent_store is not None else None)
    if read_failed:
        dialog.remember_checkbox.setEnabled(False)
        dialog.remember_error.setText(tr("Saved warning choices are unavailable. Confirm this connection once."))
        dialog.remember_error.show()
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        schedule_delete_later(dialog)
