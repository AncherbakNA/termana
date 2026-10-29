"""Calm, transfer-specific confirmations without filesystem side effects."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QResizeEvent
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
    QStyle,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.qt_lifecycle import schedule_delete_later
from termana.storage.connection_transfer import ConnectionTransferStats
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.wrapping_label import WrappingLabel

MINIMUM_TRANSFER_DIALOG_WIDTH: Final = 560
DEFAULT_TRANSFER_DIALOG_WIDTH: Final = 640
DEFAULT_TRANSFER_DIALOG_HEIGHT: Final = 400
DEFAULT_SCOPED_EXPORT_DIALOG_HEIGHT: Final = 600
TRANSFER_DIALOG_SCREEN_MARGIN: Final = 80
MAX_TRANSFER_DISPLAY_CHARACTERS: Final = 4_096
EXPORT_CONNECTIONS_NOTICE: Final = (
    "Termana will create a JSON file containing connection names, addresses, ports, "
    "and folders, plus favorite marks, SSH options, and Telnet text encodings. "
    "SSH routes, including jump host addresses, ports, and gateway usernames, are included. "
    "The file is not encrypted. Passwords and saved destination logins are not included. "
    "Local SSH key paths and key/agent choices are not exported. "
    "Local tunnel rules and permission to start them are not exported. "
    "RDP display preferences are included; RDP login hints and certificate decisions are not."
)
ENTIRE_FOLDER_EXPORT_NOTICE: Final = (
    "The entire selected folder will be exported, including all connections and "
    "subfolders, empty subfolders, and items hidden by search or Favorites filters."
)
EXISTING_CONNECTIONS_UNCHANGED_NOTICE: Final = "Existing connections remain unchanged."
_DIRECTIONAL_FORMATS: Final = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)


def _safe_transfer_display_text(value: str) -> str:
    """Make an untrusted file label visibly inert and bounded for Qt layout."""
    if not isinstance(value, str):
        raise TypeError("transfer display text must be a string")
    sanitized = "".join(
        "�"
        if unicodedata.category(character).startswith("C")
        or unicodedata.bidirectional(character) in _DIRECTIONAL_FORMATS
        else character
        for character in value
    )
    if len(sanitized) <= MAX_TRANSFER_DISPLAY_CHARACTERS:
        return sanitized
    return sanitized[: MAX_TRANSFER_DISPLAY_CHARACTERS - 1] + "…"


def _safe_path_text(path: Path) -> str:
    """Return a plain, bounded representation of one caller-selected path."""
    if not isinstance(path, Path):
        raise TypeError("connection transfer path must be a Path")
    return _safe_transfer_display_text(str(path))


class ConnectionTransferConfirmationDialog(QDialog):
    """Present one connection-transfer decision with a pinned safe action."""

    def __init__(
        self,
        *,
        object_name: str,
        window_title: str,
        title: str,
        subtitle: str,
        body: str,
        accessible_name: str,
        accessible_description: str,
        primary_text: str,
        primary_object_name: str,
        primary_accessible_name: str,
        cancel_object_name: str,
        cancel_accessible_name: str,
        parent: QWidget | None = None,
    ) -> None:
        """Build a resizable plain-text dialog whose default always rejects."""
        super().__init__(parent)

        self.setObjectName(object_name)
        self.setProperty("dialogRole", "actionNotice")
        self.setWindowTitle(window_title)
        self.setAccessibleName(accessible_name)
        self.setAccessibleDescription(accessible_description)
        self.setModal(True)
        self.setMinimumWidth(MINIMUM_TRANSFER_DIALOG_WIDTH)
        self.setSizeGripEnabled(True)
        self.resize(DEFAULT_TRANSFER_DIALOG_WIDTH, DEFAULT_TRANSFER_DIALOG_HEIGHT)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("connectionTransferBodyScroll")
        self.body_scroll.setAccessibleName(tr("Connection transfer details"))
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setMinimumHeight(96)
        self.body_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.body_scroll.setAutoFillBackground(False)
        self.body_scroll.viewport().setObjectName("connectionTransferBodyViewport")
        self.body_scroll.viewport().setAutoFillBackground(False)

        self.body_widget = QWidget()
        self.body_widget.setObjectName("connectionTransferBody")
        self.body_widget.setAutoFillBackground(False)
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

        badge_symbol = QLabel("↕")
        badge_symbol.setObjectName("actionDialogBadgeSymbol")
        badge_symbol.setTextFormat(Qt.TextFormat.PlainText)
        badge_symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge_symbol.setAccessibleName(tr("Connection transfer notice"))
        scale_widget_font(badge_symbol, 1.45, weight=QFont.Weight.Bold)
        badge_layout.addWidget(badge_symbol)

        heading_layout = QVBoxLayout()
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(5)

        self.title_label = self._plain_wrapping_label(title, "actionDialogTitle")
        self.title_label.setAccessibleName(tr("Connection transfer confirmation"))
        scale_widget_font(self.title_label, 1.5, weight=QFont.Weight.Bold)

        self.subtitle_label = QLineEdit(subtitle)
        self.subtitle_label.setObjectName("connectionTransferPath")
        self.subtitle_label.setReadOnly(True)
        self.subtitle_label.setMinimumWidth(0)
        self.subtitle_label.setToolTip(plain_text_tooltip(subtitle))
        self.subtitle_label.setCursorPosition(0)
        self.subtitle_label.setAccessibleName(tr("Connection transfer file path"))

        heading_layout.addWidget(self.title_label)
        heading_layout.addWidget(self.subtitle_label)
        header_layout.addWidget(badge)
        header_layout.addLayout(heading_layout, 1)
        body_layout.addLayout(header_layout)

        self.body_label = self._plain_wrapping_label(
            body,
            "actionDialogBody",
            selectable=True,
        )
        self.body_label.setAccessibleName(tr("Connection transfer summary"))
        body_layout.addWidget(self.body_label)
        body_layout.addStretch()

        self.body_scroll.setWidget(self.body_widget)
        root_layout.addWidget(self.body_scroll, 1)

        self.footer_widget = QWidget()
        self.footer_widget.setObjectName("connectionTransferFooter")
        footer_layout = QVBoxLayout(self.footer_widget)
        footer_layout.setContentsMargins(28, 14, 28, 22)
        footer_layout.setSpacing(10)

        self.button_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.button_layout.setContentsMargins(0, 0, 0, 0)
        self.button_layout.setSpacing(10)
        self.button_layout.addStretch()

        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setObjectName(cancel_object_name)
        self.cancel_button.setProperty("actionRole", "secondary")
        self.cancel_button.setAccessibleName(cancel_accessible_name)
        self.cancel_button.setAccessibleDescription(
            tr("Close this confirmation without transferring connections.")
        )
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)

        self.primary_button = QPushButton(primary_text)
        self.primary_button.setObjectName(primary_object_name)
        self.primary_button.setProperty("actionRole", "primary")
        self.primary_button.setAccessibleName(primary_accessible_name)
        self.primary_button.setAccessibleDescription(accessible_description)
        self.primary_button.setAutoDefault(False)
        self.primary_button.setDefault(False)
        self.primary_button.clicked.connect(self.accept)

        self.button_layout.addWidget(self.cancel_button)
        self.button_layout.addWidget(self.primary_button)
        footer_layout.addLayout(self.button_layout)
        root_layout.addWidget(self.footer_widget)

        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)

    @classmethod
    def for_export(
        cls,
        *,
        destination_path: Path,
        stats: ConnectionTransferStats | None = None,
        scope_label: str | None = None,
        include_folder_contents: bool = False,
        parent: QWidget | None = None,
    ) -> ConnectionTransferConfirmationDialog:
        """Describe an already selected export snapshot without performing I/O."""
        if stats is not None:
            if not isinstance(stats, ConnectionTransferStats):
                raise TypeError("stats must be ConnectionTransferStats or None")
            if stats.renamed_folders:
                raise ValueError("export stats cannot contain renamed folders")
        if type(include_folder_contents) is not bool:
            raise TypeError("include_folder_contents must be a bool")
        if scope_label is not None:
            if not isinstance(scope_label, str):
                raise TypeError("scope_label must be a string or None")
            if not scope_label.strip():
                raise ValueError("scope_label cannot be empty")
        safe_path = _safe_path_text(destination_path)
        has_export_details = stats is not None or scope_label is not None or include_folder_contents
        body_parts = []
        if has_export_details:
            default_scope = (
                tr("Selected folder and its contents")
                if include_folder_contents
                else tr("All saved connections")
            )
            safe_scope = _safe_transfer_display_text(
                scope_label if scope_label is not None else default_scope
            )
            body_parts.append(tr("Scope · {p0}", p0=f"{safe_scope}"))
        if stats is not None:
            body_parts.append(
                tr(
                    "Ready to export:\nSSH connections: {p0}\nTelnet connections: {p1}\nFolders: {p2}",
                    p0=f"{stats.ssh_connections}",
                    p1=f"{stats.telnet_connections}",
                    p2=f"{stats.folders}",
                )
            )
        if include_folder_contents:
            body_parts.append(tr(ENTIRE_FOLDER_EXPORT_NOTICE))
        if stats is not None and stats.rdp_connections:
            body_parts.append(tr("RDP connections: {p0}", p0=stats.rdp_connections))
        body_parts.append(tr(EXPORT_CONNECTIONS_NOTICE))
        dialog = cls(
            object_name="exportConnectionsConfirmationDialog",
            window_title=tr("Export connections?"),
            title=tr("Export connections?")
            if has_export_details
            else tr("Export all connections?"),
            subtitle=tr("Destination · {p0}", p0=f"{safe_path}"),
            body="\n\n".join(body_parts),
            accessible_name=tr("Confirm connection export"),
            accessible_description=(
                tr("Review what the unencrypted JSON export contains before continuing.")
            ),
            primary_text=tr("Export"),
            primary_object_name="confirmExportConnectionsButton",
            primary_accessible_name=(
                tr("Export selected items") if has_export_details else tr("Export all connections")
            ),
            cancel_object_name="cancelExportConnectionsButton",
            cancel_accessible_name=tr("Cancel connection export"),
            parent=parent,
        )
        if has_export_details:
            dialog._fit_initial_export_height()
        return dialog

    def _fit_initial_export_height(self) -> None:
        """Measure native fonts once; preserve later user resizing and the safe footer."""
        self.ensurePolished()
        body_layout = self.body_widget.layout()
        assert body_layout is not None
        # Reserve the native scrollbar width while measuring: an initially
        # scrollable viewport wraps more text and otherwise sustains its own
        # scrollbar even when the unscrolled width would fit the notice.
        scrollbar_width = self.body_scroll.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent, None, self.body_scroll
        )
        body_width = (
            DEFAULT_TRANSFER_DIALOG_WIDTH - 2 * self.body_scroll.frameWidth() - scrollbar_width
        )
        body_height = max(
            body_layout.heightForWidth(body_width),
            body_layout.minimumSize().height(),
        )
        required_height = body_height + self.footer_widget.sizeHint().height()
        screen = self.screen()
        available_height = (
            max(1, screen.availableGeometry().height() - TRANSFER_DIALOG_SCREEN_MARGIN)
            if screen is not None
            else DEFAULT_SCOPED_EXPORT_DIALOG_HEIGHT
        )
        self.resize(
            DEFAULT_TRANSFER_DIALOG_WIDTH,
            min(max(DEFAULT_SCOPED_EXPORT_DIALOG_HEIGHT, required_height), available_height),
        )

    @classmethod
    def for_import(
        cls,
        *,
        source_path: Path,
        stats: ConnectionTransferStats,
        parent: QWidget | None = None,
    ) -> ConnectionTransferConfirmationDialog:
        """Build the no-I/O import confirmation from already parsed statistics."""
        if not isinstance(stats, ConnectionTransferStats):
            raise TypeError("stats must be ConnectionTransferStats")
        safe_path = _safe_path_text(source_path)
        body = tr(
            "Ready to add:\nSSH connections: {p0}\nTelnet connections: {p1}\nFolders: {p2}\nRenamed folders: {p3}\n\n{p4}",
            p0=f"{stats.ssh_connections}",
            p1=f"{stats.telnet_connections}",
            p2=f"{stats.folders}",
            p3=f"{stats.renamed_folders}",
            p4=f"{tr(EXISTING_CONNECTIONS_UNCHANGED_NOTICE)}",
        )
        if stats.rdp_connections:
            body = tr("RDP connections: {p0}", p0=stats.rdp_connections) + "\n" + body
        return cls(
            object_name="importConnectionsConfirmationDialog",
            window_title=tr("Import connections?"),
            title=tr("Import these connections?"),
            subtitle=tr("Source · {p0}", p0=f"{safe_path}"),
            body=body,
            accessible_name=tr("Confirm connection import"),
            accessible_description=(
                tr("Review the parsed connection counts before adding them to the catalog.")
            ),
            primary_text=tr("Import"),
            primary_object_name="confirmImportConnectionsButton",
            primary_accessible_name=tr("Import parsed connections"),
            cancel_object_name="cancelImportConnectionsButton",
            cancel_accessible_name=tr("Cancel connection import"),
            parent=parent,
        )

    @staticmethod
    def _plain_wrapping_label(
        text: str,
        object_name: str,
        *,
        selectable: bool = False,
    ) -> QLabel:
        """Create an explicitly plain-text label suitable for untrusted values."""
        label = WrappingLabel(text)
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if selectable:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Stack actions when large text makes the horizontal footer too narrow."""
        super().resizeEvent(event)
        footer_layout = self.footer_widget.layout()
        if footer_layout is None:
            return
        margins = footer_layout.contentsMargins()
        available_width = max(
            0,
            self.footer_widget.width() - margins.left() - margins.right(),
        )
        required_width = (
            self.cancel_button.sizeHint().width()
            + self.primary_button.sizeHint().width()
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


def confirm_connection_export(
    *,
    destination_path: Path,
    stats: ConnectionTransferStats | None = None,
    scope_label: str | None = None,
    include_folder_contents: bool = False,
    parent: QWidget | None = None,
    dialog_created: Callable[[ConnectionTransferConfirmationDialog], bool] | None = None,
) -> bool:
    """Ask before exporting an unencrypted, explicitly non-secret JSON file."""
    dialog = ConnectionTransferConfirmationDialog.for_export(
        destination_path=destination_path,
        stats=stats,
        scope_label=scope_label,
        include_folder_contents=include_folder_contents,
        parent=parent,
    )
    try:
        if dialog_created is not None and not dialog_created(dialog):
            return False
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        schedule_delete_later(dialog)


def confirm_connection_import(
    *,
    source_path: Path,
    stats: ConnectionTransferStats,
    parent: QWidget | None = None,
    dialog_created: Callable[[ConnectionTransferConfirmationDialog], bool] | None = None,
) -> bool:
    """Ask before adding an already parsed transfer document to the catalog."""
    dialog = ConnectionTransferConfirmationDialog.for_import(
        source_path=source_path,
        stats=stats,
        parent=parent,
    )
    try:
        if dialog_created is not None and not dialog_created(dialog):
            return False
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        schedule_delete_later(dialog)
