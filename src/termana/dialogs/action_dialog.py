"""Reusable, calm confirmation dialog for explicit user decisions."""

from __future__ import annotations

from typing import Final, Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.ui_typography import scale_widget_font
from termana.widgets.wrapping_label import WrappingLabel

ActionDialogTone = Literal["notice", "secure"]
ActionDialogPrimaryRole = Literal["primary", "danger"]

MINIMUM_DIALOG_WIDTH: Final = 520
_VALID_TONES: Final = frozenset({"notice", "secure"})
_VALID_PRIMARY_ROLES: Final = frozenset({"primary", "danger"})


class ActionDialog(QDialog):
    """Present one focused choice with an intentionally safe default action."""

    def __init__(
        self,
        *,
        object_name: str,
        window_title: str,
        title: str,
        subtitle: str,
        body: str,
        primary_text: str,
        primary_object_name: str,
        secondary_text: str,
        secondary_object_name: str,
        tone: ActionDialogTone = "notice",
        primary_role: ActionDialogPrimaryRole = "primary",
        badge_text: str = "?",
        parent: QWidget | None = None,
    ) -> None:
        """Build a modal action notice whose Enter and Escape paths both reject."""
        super().__init__(parent)
        if tone not in _VALID_TONES:
            raise ValueError(f"Unsupported action dialog tone: {tone}")
        if primary_role not in _VALID_PRIMARY_ROLES:
            raise ValueError(f"Unsupported primary action role: {primary_role}")

        self.setObjectName(object_name)
        self.setProperty("dialogRole", "actionNotice")
        self.setWindowTitle(window_title)
        self.setModal(True)
        self.setMinimumWidth(MINIMUM_DIALOG_WIDTH)
        self.setSizeGripEnabled(False)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(26, 24, 26, 22)
        root_layout.setSpacing(18)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(15)
        header_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.badge = QFrame()
        self.badge.setObjectName("actionDialogBadge")
        self.badge.setProperty("tone", tone)
        self.badge.setFixedSize(44, 44)
        badge_layout = QVBoxLayout(self.badge)
        badge_layout.setContentsMargins(0, 0, 0, 0)

        self.badge_symbol = QLabel(badge_text)
        self.badge_symbol.setObjectName("actionDialogBadgeSymbol")
        self.badge_symbol.setProperty("tone", tone)
        self.badge_symbol.setTextFormat(Qt.TextFormat.PlainText)
        self.badge_symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge_symbol.setAccessibleName(tr("SSH action notice"))
        scale_widget_font(self.badge_symbol, 1.55, weight=QFont.Weight.Bold)
        badge_layout.addWidget(self.badge_symbol)

        heading_layout = QVBoxLayout()
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(5)

        self.title_label = self._wrapping_label(title, "actionDialogTitle")
        scale_widget_font(self.title_label, 1.55, weight=QFont.Weight.Bold)

        self.subtitle_label = self._wrapping_label(subtitle, "actionDialogSubtitle")
        self.subtitle_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        heading_layout.addWidget(self.title_label)
        heading_layout.addWidget(self.subtitle_label)
        header_layout.addWidget(self.badge)
        header_layout.addLayout(heading_layout, 1)
        root_layout.addLayout(header_layout)

        self.body_label = self._wrapping_label(body, "actionDialogBody")
        root_layout.addWidget(self.body_label)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 2, 0, 0)
        button_layout.setSpacing(10)
        button_layout.addStretch()

        self.secondary_button = QPushButton(secondary_text)
        self.secondary_button.setObjectName(secondary_object_name)
        self.secondary_button.setProperty("actionRole", "secondary")
        self.secondary_button.setAutoDefault(True)
        self.secondary_button.setDefault(True)
        self.secondary_button.clicked.connect(self.reject)

        self.primary_button = QPushButton(primary_text)
        self.primary_button.setObjectName(primary_object_name)
        self.primary_button.setProperty("actionRole", primary_role)
        self.primary_button.setAutoDefault(False)
        self.primary_button.setDefault(False)
        self.primary_button.clicked.connect(self.accept)

        button_layout.addWidget(self.secondary_button)
        button_layout.addWidget(self.primary_button)
        root_layout.addLayout(button_layout)

        self.secondary_button.setFocus(Qt.FocusReason.OtherFocusReason)

    @staticmethod
    def _wrapping_label(text: str, object_name: str) -> QLabel:
        """Create a bounded plain-text label that also behaves at large font sizes."""
        label = WrappingLabel(text)
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        return label
