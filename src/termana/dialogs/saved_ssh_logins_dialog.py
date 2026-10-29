"""Presentation-only manager for non-secret saved SSH login references."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import Final
from uuid import UUID

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAccessible,
    QAccessibleEvent,
    QCloseEvent,
    QFont,
    QKeySequence,
    QShortcut,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import count_noun, tr
from termana.models.ssh_credential import MAX_SSH_CREDENTIALS, SshCredentialRef
from termana.qt_lifecycle import schedule_delete_later
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.clipboard_text import copy_plain_text
from termana.widgets.wrapping_label import WrappingLabel

MINIMUM_DIALOG_WIDTH: Final = 600
MINIMUM_DIALOG_HEIGHT: Final = 380
MAX_OPERATION_ERROR_CHARACTERS: Final = 500
_REPLACEMENT_CHARACTER: Final = "\N{REPLACEMENT CHARACTER}"


def _format_endpoint(credential: SshCredentialRef) -> str:
    """Format an exact endpoint without leaving an IPv6 address ambiguous."""
    host = credential.endpoint.host
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{credential.endpoint.port}"


def _filter_key(value: str) -> str:
    """Normalize display text for predictable case-insensitive filtering."""
    return unicodedata.normalize("NFC", value).casefold()


def _safe_operation_error(message: object) -> str:
    """Bound caller-supplied plain text and neutralize active control formatting."""
    if not isinstance(message, str):
        raise TypeError("Saved SSH login operation error must be text")
    bounded = message[:MAX_OPERATION_ERROR_CHARACTERS]
    return "".join(
        _REPLACEMENT_CHARACTER
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"} and character not in {"\n", "\t"}
        else character
        for character in bounded
    )


class SavedSshLoginsDialog(QDialog):
    """List and request removal of saved SSH identities without reading secrets."""

    remove_requested = Signal(object)
    remove_all_requested = Signal(object)

    def __init__(
        self,
        credentials: Sequence[SshCredentialRef],
        parent: QWidget | None = None,
    ) -> None:
        """Build the manager from validated non-secret credential references."""
        super().__init__(parent)
        self._credentials: tuple[SshCredentialRef, ...] = ()
        self._operation_pending = False
        self._filter_state: str | None = None
        self._filter_status_message = ""
        self._button_layout_pending = False
        self._error_reveal_pending = False

        self.setObjectName("savedSshLoginsDialog")
        self.setWindowTitle(tr("Saved SSH logins"))
        self.setAccessibleName(tr("Saved SSH logins"))
        self.setAccessibleDescription(
            tr("View or remove SSH login references recorded when passwords were saved.")
        )
        self.setModal(True)
        self.setMinimumSize(MINIMUM_DIALOG_WIDTH, MINIMUM_DIALOG_HEIGHT)
        self.setSizeGripEnabled(True)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(24, 22, 24, 20)
        root_layout.setSpacing(16)

        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("savedSshLoginsBodyScroll")
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setMinimumHeight(96)
        self.body_widget = QWidget()
        self.body_widget.setMinimumWidth(0)
        body_layout = QVBoxLayout(self.body_widget)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(16)
        self.body_scroll.setWidget(self.body_widget)
        root_layout.addWidget(self.body_scroll, 1)

        title = WrappingLabel(tr("Saved SSH logins"))
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setObjectName("savedSshLoginsDialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        scale_widget_font(title, 1.55, weight=QFont.Weight.Bold)
        body_layout.addWidget(title)

        description = WrappingLabel(
            tr(
                "Termana lists the SSH endpoint and username recorded when you chose to save a password. Passwords stay in your operating system and are not read when this list opens."
            )
        )
        description.setObjectName("savedSshLoginsDescription")
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        body_layout.addWidget(description)

        self.filter_input = QLineEdit()
        self.filter_input.setObjectName("savedSshLoginFilterInput")
        self.filter_input.setPlaceholderText(tr("Filter by host or username"))
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.setMaxLength(512)
        self.filter_input.setAccessibleName(tr("Filter saved SSH logins"))
        self.filter_input.setAccessibleDescription(
            tr("Filter saved SSH logins by endpoint, port, or username.")
        )
        body_layout.addWidget(self.filter_input)

        self.credentials_tree = QTreeWidget()
        self.credentials_tree.setObjectName("savedSshLoginsTree")
        self.credentials_tree.setAccessibleName(tr("Saved SSH logins"))
        self.credentials_tree.setAccessibleDescription(
            tr("SSH endpoints and usernames recorded when passwords were saved.")
        )
        self.credentials_tree.setColumnCount(2)
        self.credentials_tree.setHeaderLabels((tr("SSH endpoint"), tr("Username")))
        self.credentials_tree.setRootIsDecorated(False)
        self.credentials_tree.setItemsExpandable(False)
        self.credentials_tree.setUniformRowHeights(True)
        self.credentials_tree.setAllColumnsShowFocus(True)
        self.credentials_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.credentials_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.credentials_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.credentials_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.credentials_tree.header().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.credentials_tree.setMinimumHeight(140)
        body_layout.addWidget(self.credentials_tree, 1)

        self.state_panel = QFrame()
        self.state_panel.setObjectName("savedSshLoginsStatePanel")
        state_layout = QVBoxLayout(self.state_panel)
        state_layout.setContentsMargins(20, 26, 20, 26)
        state_layout.setSpacing(6)

        self.state_title = WrappingLabel()
        self.state_title.setObjectName("savedSshLoginsStateTitle")
        self.state_title.setTextFormat(Qt.TextFormat.PlainText)
        self.state_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_title.setWordWrap(True)
        scale_widget_font(self.state_title, 1.25, weight=QFont.Weight.DemiBold)
        state_layout.addWidget(self.state_title)

        self.state_description = WrappingLabel()
        self.state_description.setObjectName("savedSshLoginsStateDescription")
        self.state_description.setTextFormat(Qt.TextFormat.PlainText)
        self.state_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_description.setWordWrap(True)
        state_layout.addWidget(self.state_description)
        body_layout.addWidget(self.state_panel, 1)

        self.count_label = QLabel()
        self.count_label.setObjectName("savedSshLoginsCount")
        self.count_label.setTextFormat(Qt.TextFormat.PlainText)
        body_layout.addWidget(self.count_label)

        self.operation_status_label = WrappingLabel()
        self.operation_status_label.setObjectName("savedSshLoginsOperationStatus")
        self.operation_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.operation_status_label.setWordWrap(True)
        self.operation_status_label.hide()
        body_layout.addWidget(self.operation_status_label)

        self.error_view = QPlainTextEdit()
        self.error_view.setObjectName("savedSshLoginsError")
        self.error_view.setReadOnly(True)
        self.error_view.setTabChangesFocus(True)
        self.error_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.error_view.setMinimumHeight(80)
        self.error_view.setMaximumHeight(140)
        self.error_view.hide()
        body_layout.addWidget(self.error_view)

        self.button_box = QDialogButtonBox()
        self.button_box.setObjectName("savedSshLoginsDialogButtons")
        self.button_box.setOrientation(Qt.Orientation.Vertical)
        self.copy_username_button = self.button_box.addButton(
            tr("Copy username"), QDialogButtonBox.ButtonRole.ActionRole
        )
        self.copy_endpoint_button = self.button_box.addButton(
            tr("Copy address"), QDialogButtonBox.ButtonRole.ActionRole
        )
        for button, kind in (
            (self.copy_username_button, "username"),
            (self.copy_endpoint_button, "address"),
        ):
            button.setObjectName(f"copySavedSshLogin{kind.title()}Button")
            button.setProperty("actionRole", "secondary")
            button.setAccessibleName(
                tr(
                    "Copy selected SSH {p0}; no password is read",
                    p0=tr(kind, context="ssh-reference"),
                )
            )
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, kind=kind: self._copy_reference(kind))

        self.remove_button = self.button_box.addButton(
            tr("Remove login…"),
            QDialogButtonBox.ButtonRole.DestructiveRole,
        )
        self.remove_button.setObjectName("removeSavedSshLoginButton")
        self.remove_button.setAccessibleName(tr("Remove the selected saved SSH login"))
        self.remove_button.setEnabled(False)

        self.remove_all_button = self.button_box.addButton(
            tr("Remove all…"),
            QDialogButtonBox.ButtonRole.DestructiveRole,
        )
        self.remove_all_button.setObjectName("removeAllSavedSshLoginsButton")
        self.remove_all_button.setAccessibleName(tr("Remove all saved SSH logins"))
        self.remove_all_button.setEnabled(False)

        self.close_button = self.button_box.addButton(
            tr("Close"),
            QDialogButtonBox.ButtonRole.RejectRole,
        )
        self.close_button.setObjectName("closeSavedSshLoginsButton")
        self.close_button.setAccessibleName(tr("Close saved SSH logins"))
        self.close_button.setAutoDefault(True)
        self.close_button.setDefault(True)
        root_layout.addWidget(self.button_box)
        self.button_box.installEventFilter(self)
        self.body_widget.installEventFilter(self)
        self.body_scroll.viewport().installEventFilter(self)

        self.filter_input.textChanged.connect(self._apply_filter)
        self.credentials_tree.itemSelectionChanged.connect(self._selection_changed)
        self.remove_button.clicked.connect(self._request_selected_removal)
        self.remove_all_button.clicked.connect(self._request_all_removal)
        self.close_button.clicked.connect(self.reject)
        self.filter_input.installEventFilter(self)
        self.credentials_tree.installEventFilter(self)
        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self.find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.find_shortcut.activated.connect(self._focus_filter)

        QWidget.setTabOrder(self.filter_input, self.credentials_tree)
        QWidget.setTabOrder(self.credentials_tree, self.copy_username_button)
        QWidget.setTabOrder(self.copy_username_button, self.copy_endpoint_button)
        QWidget.setTabOrder(self.copy_endpoint_button, self.remove_button)
        QWidget.setTabOrder(self.remove_button, self.remove_all_button)
        QWidget.setTabOrder(self.remove_all_button, self.close_button)

        self.set_credentials(credentials)

    def eventFilter(self, watched, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.KeyPress
            and not event.modifiers()
            and not self._operation_pending
        ):
            if watched is self.filter_input and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                self._move_selection(-1 if event.key() == Qt.Key.Key_Up else 1)
                return True
            if (
                watched in (self.filter_input, self.credentials_tree)
                and event.key() == Qt.Key.Key_Escape
                and self.filter_input.text()
            ):
                self.filter_input.clear()
                self._focus_filter()
                return True
        if (
            watched in (self.body_widget, self.body_scroll.viewport())
            and event.type() == QEvent.Type.Resize
            and self.error_view.isVisible()
        ):
            self._schedule_error_reveal()
        if watched is self.button_box and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ):
            if not self._button_layout_pending:
                self._button_layout_pending = True
                QTimer.singleShot(0, self, self._update_button_layout)
        return super().eventFilter(watched, event)

    def _update_button_layout(self) -> None:
        """Keep every action reachable when large text needs stacked buttons."""
        self._button_layout_pending = False
        required = sum(button.sizeHint().width() for button in self.button_box.buttons())
        required += max(0, self.button_box.layout().spacing()) * (
            len(self.button_box.buttons()) - 1
        )
        orientation = (
            Qt.Orientation.Vertical
            if required > self.button_box.width()
            else Qt.Orientation.Horizontal
        )
        if self.button_box.orientation() != orientation:
            self.button_box.setOrientation(orientation)

    @property
    def credentials(self) -> tuple[SshCredentialRef, ...]:
        """Return the currently presented immutable non-secret snapshot."""
        return self._credentials

    def set_credentials(self, credentials: Sequence[SshCredentialRef]) -> None:
        """Replace rows after the owner has completed a repository operation."""
        validated = self._validated_credentials(credentials)
        selected_id = self._selected_credential_id()
        self._operation_pending = False
        self._credentials = tuple(
            sorted(
                validated,
                key=lambda credential: (
                    _filter_key(credential.endpoint.host),
                    credential.endpoint.host,
                    credential.endpoint.port,
                    _filter_key(credential.username),
                    credential.username,
                ),
            )
        )

        self.credentials_tree.clear()
        for credential in self._credentials:
            endpoint = _format_endpoint(credential)
            item = QTreeWidgetItem((endpoint, credential.username))
            item.setData(0, Qt.ItemDataRole.UserRole, credential.credential_id)
            item.setData(
                0,
                Qt.ItemDataRole.AccessibleTextRole,
                tr(
                    "Saved SSH login for {p0} on {p1}",
                    p0=f"{credential.username}",
                    p1=f"{endpoint}",
                ),
            )
            item.setToolTip(0, plain_text_tooltip(endpoint))
            item.setToolTip(1, plain_text_tooltip(credential.username))
            self.credentials_tree.addTopLevelItem(item)

        self._apply_filter(self.filter_input.text())

        restored_item = self._item_for_credential_id(selected_id)
        if restored_item is not None and not restored_item.isHidden():
            self.credentials_tree.setCurrentItem(restored_item)
        elif self.credentials_tree.isVisible():
            first_visible = self._first_visible_item()
            if first_visible is not None:
                self.credentials_tree.setCurrentItem(first_visible)
        self._selection_changed()
        self._refresh_operation_controls()

        self._hide_operation_status()
        if not self.credentials_tree.isHidden():
            self.credentials_tree.setFocus(Qt.FocusReason.OtherFocusReason)
        elif self._credentials:
            self.filter_input.setFocus(Qt.FocusReason.OtherFocusReason)
            self.filter_input.selectAll()
        else:
            self.close_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_operation_error(self, message: str) -> None:
        """Expose one bounded plain-text failure while retaining all rows."""
        safe_message = _safe_operation_error(message)
        self._operation_pending = False
        self._refresh_operation_controls()
        self._hide_operation_status()
        self.error_view.setPlainText(safe_message)
        self.error_view.setAccessibleName(tr("Saved SSH login error: {p0}", p0=f"{safe_message}"))
        self.error_view.setVisible(bool(safe_message))
        if safe_message:
            QAccessible.updateAccessibility(
                QAccessibleEvent(self.error_view, QAccessible.Event.Alert)
            )
            self._schedule_error_reveal()
        if self.remove_button.isEnabled():
            self.remove_button.setFocus(Qt.FocusReason.OtherFocusReason)
        elif not self.credentials_tree.isHidden():
            self.credentials_tree.setFocus(Qt.FocusReason.OtherFocusReason)
        elif self._credentials:
            self.filter_input.setFocus(Qt.FocusReason.OtherFocusReason)
            self.filter_input.selectAll()
        else:
            self.close_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def clear_operation_error(self) -> None:
        """Hide feedback that no longer describes the current operation."""
        self.error_view.clear()
        self.error_view.setAccessibleName("")
        self.error_view.hide()

    def _reveal_operation_error(self) -> None:
        self._error_reveal_pending = False
        if self.error_view.isVisible():
            # ensureWidgetVisible follows a text editor's cursor rectangle, which
            # can leave half the error panel below the viewport. Reveal its edge.
            self.body_scroll.ensureVisible(0, self.error_view.geometry().bottom(), 0, 12)

    def _schedule_error_reveal(self) -> None:
        if not self._error_reveal_pending:
            self._error_reveal_pending = True
            QTimer.singleShot(0, self, self._reveal_operation_error)

    @staticmethod
    def _validated_credentials(
        credentials: Sequence[SshCredentialRef],
    ) -> tuple[SshCredentialRef, ...]:
        try:
            validated = tuple(credentials)
        except TypeError:
            raise ValueError("Saved SSH logins must be a sequence") from None
        if len(validated) > MAX_SSH_CREDENTIALS:
            raise ValueError("Too many saved SSH logins to display")
        if any(not isinstance(credential, SshCredentialRef) for credential in validated):
            raise ValueError("Saved SSH logins contain an unsupported value")
        credential_ids = [credential.credential_id for credential in validated]
        if len(credential_ids) != len(set(credential_ids)):
            raise ValueError("Saved SSH logins contain duplicate credential IDs")
        identities = [(credential.endpoint, credential.username) for credential in validated]
        if len(identities) != len(set(identities)):
            raise ValueError("Saved SSH logins contain duplicate endpoint and username pairs")
        return validated

    def _apply_filter(self, text: str) -> None:
        previous_state = self._filter_state
        previous_message = self._filter_status_message
        query = _filter_key(text.strip())
        visible_count = 0
        for row in range(self.credentials_tree.topLevelItemCount()):
            item = self.credentials_tree.topLevelItem(row)
            searchable = _filter_key(f"{item.text(0)}\n{item.text(1)}")
            visible = not query or query in searchable
            item.setHidden(not visible)
            visible_count += int(visible)

        total_count = len(self._credentials)
        has_results = visible_count > 0
        self.credentials_tree.setVisible(has_results)
        self.state_panel.setVisible(not has_results)
        if total_count == 0:
            filter_state = "empty"
            self.state_title.setText(tr("No saved SSH logins"))
            self.state_description.setText(
                tr("Passwords you choose to save after a successful SSH sign-in will appear here.")
            )
            self.count_label.setText(tr("0 saved logins"))
            filter_status_message = tr("No saved SSH logins")
        elif not has_results:
            filter_state = "noResults"
            self.state_title.setText(tr("No matching saved logins"))
            self.state_description.setText(tr("No saved logins match this filter."))
            self.count_label.setText(tr("0 of {p0} saved logins", p0=f"{total_count}"))
            filter_status_message = tr(
                "No filter matches out of {p0} saved SSH logins", p0=f"{total_count}"
            )
        else:
            filter_state = "results" if query else "inactive"
            self.state_title.clear()
            self.state_description.clear()
            if query:
                self.count_label.setText(
                    tr("{p0} of {p1} saved logins", p0=f"{visible_count}", p1=f"{total_count}")
                )
                filter_status_message = tr(
                    "Showing {p0} of {p1} saved SSH logins",
                    p0=f"{visible_count}",
                    p1=f"{total_count}",
                )
            else:
                label = count_noun("saved login", "saved logins", total_count)
                self.count_label.setText(f"{total_count} {label}")
                filter_status_message = (
                    tr("{p0} saved SSH login", p0=f"{total_count}")
                    if total_count == 1
                    else tr("{p0} saved SSH logins", p0=f"{total_count}")
                )
            current_item = self.credentials_tree.currentItem()
            if current_item is None or current_item.isHidden():
                first_visible = self._first_visible_item()
                if first_visible is not None:
                    self.credentials_tree.setCurrentItem(first_visible)
        self.count_label.setAccessibleName(filter_status_message)
        self._filter_state = filter_state
        self._filter_status_message = filter_status_message
        if previous_state is not None and previous_message != filter_status_message:
            event_type = (
                QAccessible.Event.Alert
                if filter_state == "noResults" and previous_state != "noResults"
                else QAccessible.Event.NameChanged
            )
            QAccessible.updateAccessibility(QAccessibleEvent(self.count_label, event_type))
        self._selection_changed()

    def _selection_changed(self) -> None:
        credential = self._selected_credential()
        for button, caption in (
            (self.copy_username_button, tr("Copy username")),
            (self.copy_endpoint_button, tr("Copy address")),
        ):
            button.setEnabled(credential is not None and not self._operation_pending)
            button.setText(caption)
            button.setAccessibleName(
                tr("{p0} for selected SSH login; no password is read", p0=f"{caption}")
            )
        self.remove_button.setEnabled(credential is not None and not self._operation_pending)
        if credential is None:
            self.remove_button.setAccessibleName(tr("Remove the selected saved SSH login"))
            return
        endpoint = _format_endpoint(credential)
        self.remove_button.setAccessibleName(
            tr(
                "Remove saved SSH login {p0} for {p1}",
                p0=f"{credential.username}",
                p1=f"{endpoint}",
            )
        )

    def _focus_filter(self) -> None:
        if self.filter_input.isEnabled() and not self._operation_pending:
            self.filter_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.filter_input.selectAll()
            self.body_scroll.ensureWidgetVisible(self.filter_input)

    def _move_selection(self, step: int) -> None:
        items = [
            self.credentials_tree.topLevelItem(row)
            for row in range(self.credentials_tree.topLevelItemCount())
        ]
        items = [item for item in items if not item.isHidden()]
        if not items:
            return
        current = self.credentials_tree.currentItem()
        index = (
            max(0, min(len(items) - 1, items.index(current) + step))
            if current in items
            else (0 if step > 0 else len(items) - 1)
        )
        self.credentials_tree.setCurrentItem(items[index])
        self.credentials_tree.scrollToItem(items[index])

    def _copy_reference(self, kind: str) -> None:
        if self._operation_pending or not self.isVisible():
            return
        credential = self._selected_credential()
        if credential is None:
            return
        text = credential.username if kind == "username" else _format_endpoint(credential)
        copied = copy_plain_text(text)
        button = self.copy_username_button if kind == "username" else self.copy_endpoint_button
        button.setText(tr("Copied") if copied else tr("Copy unavailable"))
        button.setAccessibleName(
            tr(
                "SSH {p0} copied to clipboard; no password was read",
                p0=tr(kind, context="ssh-reference"),
            )
            if copied
            else tr(
                "Clipboard unavailable; SSH {p0} not copied; no password was read",
                p0=tr(kind, context="ssh-reference"),
            )
        )

    def _request_selected_removal(self) -> None:
        if self._operation_pending:
            return
        credential = self._selected_credential()
        if credential is None or not self._confirm_selected_removal(credential):
            return
        self._begin_operation(tr("Removing saved SSH login…"))
        self.remove_requested.emit(credential.credential_id)

    def _request_all_removal(self) -> None:
        if self._operation_pending:
            return
        credential_ids = frozenset(credential.credential_id for credential in self._credentials)
        if not credential_ids or not self._confirm_all_removal(len(credential_ids)):
            return
        self._begin_operation(tr("Removing all saved SSH logins…"))
        self.remove_all_requested.emit(credential_ids)

    def _begin_operation(self, message: str) -> None:
        """Prevent duplicate destructive requests until the owner reports an outcome."""
        self._operation_pending = True
        self._refresh_operation_controls()
        self.operation_status_label.setText(message)
        self.operation_status_label.setAccessibleName(message)
        self.operation_status_label.show()
        QAccessible.updateAccessibility(
            QAccessibleEvent(self.operation_status_label, QAccessible.Event.Alert)
        )

    def _refresh_operation_controls(self) -> None:
        """Apply availability from both content and the current operation lifecycle."""
        available = bool(self._credentials) and not self._operation_pending
        self.filter_input.setEnabled(available)
        self.credentials_tree.setEnabled(available)
        self.remove_all_button.setEnabled(available)
        self.close_button.setEnabled(not self._operation_pending)
        self._selection_changed()

    def _hide_operation_status(self) -> None:
        """Clear the busy announcement after the owner reports an outcome."""
        self.operation_status_label.clear()
        self.operation_status_label.setAccessibleName("")
        self.operation_status_label.hide()

    def reject(self) -> None:
        """Keep the dialog alive while an asynchronous owner still references it."""
        if self._operation_pending:
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Ignore title-bar closure until the pending repository result arrives."""
        if self._operation_pending:
            event.ignore()
            return
        super().closeEvent(event)

    def _confirm_selected_removal(self, credential: SshCredentialRef) -> bool:
        endpoint = _format_endpoint(credential)
        message_box = QMessageBox(self)
        message_box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        message_box.setObjectName("removeSavedSshLoginDialog")
        message_box.setWindowTitle(tr("Remove saved SSH login"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(
            tr(
                'Remove the saved password for "{p0}" on "{p1}"?',
                p0=f"{credential.username}",
                p1=f"{endpoint}",
            )
        )
        message_box.setInformativeText(
            tr(
                "Saved connection profiles are not deleted. Existing connected terminals stay open, and you can enter the password manually next time."
            )
        )

        remove_button = QPushButton(tr("Remove login"), message_box)
        remove_button.setObjectName("confirmRemoveSavedSshLoginButton")
        message_box.addButton(remove_button, QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = QPushButton(tr("Cancel"), message_box)
        cancel_button.setObjectName("cancelRemoveSavedSshLoginButton")
        message_box.addButton(cancel_button, QMessageBox.ButtonRole.RejectRole)
        message_box.setDefaultButton(cancel_button)
        message_box.setEscapeButton(cancel_button)
        try:
            message_box.exec()
            return message_box.clickedButton() is remove_button
        finally:
            schedule_delete_later(message_box)

    def _confirm_all_removal(self, count: int) -> bool:
        password_label = "password" if count == 1 else "passwords"
        message_box = QMessageBox(self)
        message_box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        message_box.setObjectName("removeAllSavedSshLoginsDialog")
        message_box.setWindowTitle(tr("Remove all saved SSH logins"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(
            tr("Remove {p0} saved SSH {p1}?", p0=f"{count}", p1=f"{password_label}")
        )
        message_box.setInformativeText(
            tr(
                "Saved connection profiles are not deleted. Existing connected terminals stay open, and you can enter passwords manually next time."
            )
        )

        remove_button = QPushButton(tr("Remove all"), message_box)
        remove_button.setObjectName("confirmRemoveAllSavedSshLoginsButton")
        message_box.addButton(remove_button, QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = QPushButton(tr("Cancel"), message_box)
        cancel_button.setObjectName("cancelRemoveAllSavedSshLoginsButton")
        message_box.addButton(cancel_button, QMessageBox.ButtonRole.RejectRole)
        message_box.setDefaultButton(cancel_button)
        message_box.setEscapeButton(cancel_button)
        try:
            message_box.exec()
            return message_box.clickedButton() is remove_button
        finally:
            schedule_delete_later(message_box)

    def _selected_credential_id(self) -> UUID | None:
        item = self.credentials_tree.currentItem()
        if item is None or item.isHidden() or not item.isSelected():
            return None
        value = item.data(0, Qt.ItemDataRole.UserRole)
        return value if isinstance(value, UUID) else None

    def _selected_credential(self) -> SshCredentialRef | None:
        credential_id = self._selected_credential_id()
        if credential_id is None:
            return None
        return next(
            (
                credential
                for credential in self._credentials
                if credential.credential_id == credential_id
            ),
            None,
        )

    def _item_for_credential_id(self, credential_id: UUID | None) -> QTreeWidgetItem | None:
        if credential_id is None:
            return None
        for row in range(self.credentials_tree.topLevelItemCount()):
            item = self.credentials_tree.topLevelItem(row)
            if item.data(0, Qt.ItemDataRole.UserRole) == credential_id:
                return item
        return None

    def _first_visible_item(self) -> QTreeWidgetItem | None:
        for row in range(self.credentials_tree.topLevelItemCount()):
            item = self.credentials_tree.topLevelItem(row)
            if not item.isHidden():
                return item
        return None
