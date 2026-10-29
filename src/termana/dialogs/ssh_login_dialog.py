"""Compact SSH login chooser backed only by credential reference metadata."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from PySide6.QtCore import QEvent, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.ssh_credential import MAX_SSH_CREDENTIALS, validate_ssh_username
from termana.qt_lifecycle import schedule_delete_later
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.inline_error_area import InlineErrorArea
from termana.widgets.themed_checkbox import ThemedCheckBox

MINIMUM_DIALOG_WIDTH: Final = 470
_OPTION_ROW_HEIGHT: Final = 42
_MAX_VISIBLE_OPTION_ROWS: Final = 5
_MAX_BUTTON_USERNAME_CHARACTERS: Final = 32


def _button_username(username: str) -> str:
    """Return a bounded Qt label while retaining the exact identity elsewhere."""
    if len(username) > _MAX_BUTTON_USERNAME_CHARACTERS:
        prefix_length = (_MAX_BUTTON_USERNAME_CHARACTERS - 1) // 2
        suffix_length = _MAX_BUTTON_USERNAME_CHARACTERS - 1 - prefix_length
        username = f"{username[:prefix_length]}…{username[-suffix_length:]}"
    return username.replace("&", "&&")


def _validate_username(username: object) -> str:
    """Share the exact case-sensitive username contract with terminal entry."""
    return validate_ssh_username(username)


@dataclass(frozen=True, slots=True)
class SavedSshLoginOption:
    """Non-secret identity data needed to render one saved-login choice."""

    credential_id: UUID
    username: str

    def __post_init__(self) -> None:
        """Keep malformed credential metadata out of the widget tree."""
        if not isinstance(self.credential_id, UUID):
            raise ValueError("SSH credential ID must be a UUID")
        normalized = _validate_username(self.username)
        if normalized != self.username:
            raise ValueError("Saved SSH username cannot start or end with whitespace")


@dataclass(frozen=True, slots=True)
class SshLoginSelection:
    """The login intent returned after an explicit Connect action."""

    username: str | None
    credential_id: UUID | None
    remember_for_profile: bool

    def __post_init__(self) -> None:
        """Reject values that cannot safely enter the SSH connection flow."""
        if self.credential_id is not None and not isinstance(self.credential_id, UUID):
            raise ValueError("SSH credential ID must be a UUID or None")
        if not isinstance(self.remember_for_profile, bool):
            raise ValueError("Remember-login preference must be a boolean")
        if self.credential_id is None:
            if self.username is not None:
                raise ValueError("A manual SSH login cannot include a username yet")
            if self.remember_for_profile:
                raise ValueError("A manual SSH login cannot be remembered yet")
            return
        if self.username is None:
            raise ValueError("A saved SSH login requires a username")
        normalized = _validate_username(self.username)
        if normalized != self.username:
            raise ValueError("SSH username cannot start or end with whitespace")


class SshLoginDialog(QDialog):
    """Let the user choose a saved SSH identity or defer to terminal entry."""

    remove_requested = Signal(object)

    def __init__(
        self,
        *,
        endpoint: str,
        options: Sequence[SavedSshLoginOption],
        parent: QWidget | None = None,
    ) -> None:
        """Build a small chooser; callers must skip it when no options exist."""
        super().__init__(parent)

        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("SSH endpoint cannot be empty")
        if endpoint != endpoint.strip() or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in endpoint
        ):
            raise ValueError("SSH endpoint contains unsafe characters")

        saved_options = tuple(options)
        if not saved_options:
            raise ValueError("SSH login chooser requires at least one saved login")
        if len(saved_options) > MAX_SSH_CREDENTIALS:
            raise ValueError("Too many saved SSH logins to display")
        if any(not isinstance(option, SavedSshLoginOption) for option in saved_options):
            raise ValueError("SSH login choices contain an unsupported value")
        credential_ids = [option.credential_id for option in saved_options]
        usernames = [option.username for option in saved_options]
        if len(credential_ids) != len(set(credential_ids)):
            raise ValueError("SSH login choices contain duplicate credential IDs")
        if len(usernames) != len(set(usernames)):
            raise ValueError("SSH login choices contain duplicate usernames")

        self._saved_options = saved_options
        self._selection: SshLoginSelection | None = None
        self._saved_remember_preference = True
        self._metrics_update_pending = False
        self._removal_pending = False
        self._removal_confirmation = None
        self.installEventFilter(self)

        self.setObjectName("sshLoginDialog")
        self.setWindowTitle(tr("Choose SSH login"))
        self.setAccessibleName(tr("Choose SSH login"))
        self.setAccessibleDescription(tr("Choose a login for {p0}.", p0=f"{endpoint}"))
        self.setModal(True)
        self.setMinimumWidth(MINIMUM_DIALOG_WIDTH)
        self.setSizeGripEnabled(False)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(24, 22, 24, 22)
        root_layout.setSpacing(16)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)
        header_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        badge = QFrame()
        badge.setObjectName("sshLoginBadge")
        badge.setFixedSize(42, 42)
        badge_layout = QVBoxLayout(badge)
        badge_layout.setContentsMargins(0, 0, 0, 0)

        badge_symbol = QLabel("@")
        badge_symbol.setObjectName("sshLoginBadgeSymbol")
        badge_symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge_symbol.setAccessibleName(tr("SSH login"))
        scale_widget_font(badge_symbol, 1.5, weight=QFont.Weight.Bold)
        badge_layout.addWidget(badge_symbol)

        heading_layout = QVBoxLayout()
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(4)

        title = QLabel(tr("Choose a login"))
        title.setObjectName("sshLoginDialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        scale_widget_font(title, 1.55, weight=QFont.Weight.Bold)

        self.endpoint_label = QLabel(endpoint)
        self.endpoint_label.setObjectName("sshLoginEndpointLabel")
        self.endpoint_label.setTextFormat(Qt.TextFormat.PlainText)
        self.endpoint_label.setWordWrap(True)
        self.endpoint_label.setMinimumWidth(0)
        self.endpoint_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.endpoint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.endpoint_label.setAccessibleName(tr("SSH endpoint: {p0}", p0=f"{endpoint}"))
        self.endpoint_label.setToolTip(plain_text_tooltip(endpoint))

        heading_layout.addWidget(title)
        heading_layout.addWidget(self.endpoint_label)
        header_layout.addWidget(badge)
        header_layout.addLayout(heading_layout, 1)
        root_layout.addLayout(header_layout)

        description = QLabel(
            tr(
                "Choose a saved login or enter another username. Passwords are not unlocked to display this list."
            )
        )
        description.setObjectName("sshLoginDescription")
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        root_layout.addWidget(description)

        self.options_list = QListWidget()
        self.options_list.setObjectName("sshLoginOptions")
        self.options_list.setAccessibleName(tr("Available SSH logins"))
        self.options_list.setAccessibleDescription(
            tr("Saved usernames followed by an option to enter another username.")
        )
        self.options_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.options_list.setUniformItemSizes(True)
        self.options_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.options_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)

        option_row_height = max(
            _OPTION_ROW_HEIGHT,
            self.options_list.fontMetrics().height() + 18,
        )
        for option in saved_options:
            item = QListWidgetItem(option.username)
            item.setSizeHint(QSize(0, option_row_height))
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                tr("{p0}, saved password available", p0=f"{option.username}"),
            )
            item.setToolTip(
                plain_text_tooltip(
                    tr("Connect as {p0} using the saved password", p0=f"{option.username}")
                )
            )
            self.options_list.addItem(item)

        other_item = QListWidgetItem(tr("Other user…"))
        other_item.setSizeHint(QSize(0, option_row_height))
        other_item.setData(
            Qt.ItemDataRole.AccessibleTextRole,
            tr("Enter another SSH username in the terminal"),
        )
        other_item.setToolTip(tr("Enter another username in the terminal before connecting"))
        self.options_list.addItem(other_item)
        visible_rows = min(self.options_list.count(), _MAX_VISIBLE_OPTION_ROWS)
        self.options_list.setFixedHeight(visible_rows * option_row_height + 2)
        root_layout.addWidget(self.options_list)

        self.remove_button = QPushButton(tr("Remove login…"))
        self.remove_button.setObjectName("removeSshLoginButton")
        self.remove_button.setProperty("actionRole", "secondary")
        self.remove_button.setAccessibleName(tr("Remove the selected saved SSH login"))
        self.remove_button.setToolTip(tr("Remove the selected saved SSH login (Delete)."))
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(self._request_removal)
        root_layout.addWidget(self.remove_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.operation_error = InlineErrorArea(self)
        self.operation_error.setAccessibleName(tr("Saved SSH login operation status"))
        root_layout.addWidget(self.operation_error)
        self.remove_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.options_list)
        self.remove_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.remove_shortcut.setAutoRepeat(False)
        self.remove_shortcut.activated.connect(self._request_removal)

        self.remember_checkbox = ThemedCheckBox(tr("Put this login first the next time I connect"))
        self.remember_checkbox.setObjectName("rememberSshLoginCheckbox")
        self.remember_checkbox.setAccessibleName(
            tr("Prefer the selected SSH login the next time this connection opens")
        )
        self.remember_checkbox.setAccessibleDescription(
            tr("The login chooser will still appear, with this account selected first.")
        )
        self.remember_checkbox.setChecked(True)
        root_layout.addWidget(self.remember_checkbox)

        buttons = QDialogButtonBox()
        buttons.setObjectName("sshLoginDialogButtons")
        self.cancel_button = buttons.addButton(
            tr("Cancel"),
            QDialogButtonBox.ButtonRole.RejectRole,
        )
        self.cancel_button.setObjectName("cancelSshLoginButton")
        self.cancel_button.setAccessibleName(tr("Cancel SSH connection"))
        self.connect_button = buttons.addButton(
            tr("Connect"),
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.connect_button.setObjectName("connectSshLoginButton")
        self.connect_button.setAccessibleName(tr("Connect with selected SSH login"))
        self.connect_button.setAutoDefault(True)
        # Native macOS button-box layout treats Ignored as zero horizontal
        # space, placing this fixed-width button beyond its parent's right edge.
        # Width is measured below from the complete bounded caption, not elision.
        self.connect_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.connect_button.setDefault(True)
        self.connect_button.installEventFilter(self)
        self.options_list.installEventFilter(self)
        root_layout.addWidget(buttons)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.options_list.currentRowChanged.connect(self._handle_option_changed)
        self.options_list.itemActivated.connect(lambda _item: self.accept())
        self.remember_checkbox.toggled.connect(self._remember_preference_changed)

        self.options_list.setCurrentRow(0)
        self.options_list.setFocus(Qt.FocusReason.OtherFocusReason)
        QWidget.setTabOrder(self.options_list, self.remove_button)
        QWidget.setTabOrder(self.remove_button, self.remember_checkbox)
        QWidget.setTabOrder(self.remember_checkbox, self.connect_button)
        QWidget.setTabOrder(self.connect_button, self.cancel_button)

    @property
    def selection(self) -> SshLoginSelection:
        """Return a validated choice only after the dialog was accepted."""
        if self._selection is None:
            raise RuntimeError("SSH login dialog has not been accepted")
        return self._selection

    def accept(self) -> None:
        """Freeze the visible selection into a value object before closing."""
        if self._removal_pending or self._removal_confirmation is not None:
            return
        row = self.options_list.currentRow()
        if 0 <= row < len(self._saved_options):
            option = self._saved_options[row]
            self._selection = SshLoginSelection(
                username=option.username,
                credential_id=option.credential_id,
                remember_for_profile=self.remember_checkbox.isChecked(),
            )
            super().accept()
            return

        if row != len(self._saved_options):
            self.options_list.setFocus()
            return

        self._selection = SshLoginSelection(
            username=None,
            credential_id=None,
            remember_for_profile=False,
        )
        super().accept()

    def reject(self) -> None:
        if self._removal_confirmation is not None:
            self._removal_confirmation.reject()
        super().reject()

    def _request_removal(self) -> None:
        row = self.options_list.currentRow()
        if (
            not self._removal_pending
            and self._removal_confirmation is None
            and 0 <= row < len(self._saved_options)
        ):
            self.remove_requested.emit(self._saved_options[row].credential_id)

    def set_removal_pending(self, pending: bool) -> None:
        self._removal_pending = pending
        self.options_list.setEnabled(not pending)
        self.remove_shortcut.setEnabled(not pending)
        self._handle_option_changed(self.options_list.currentRow())
        if pending:
            self.operation_error.show_message(
                tr("Removing saved login… Closing this window does not cancel the removal.")
            )
        else:
            self.operation_error.hide()

    def show_operation_error(self, message: str) -> None:
        self.operation_error.show_message(message)

    def forget_login(self, credential_id: UUID) -> bool:
        """Forget only the deleted row; never silently choose another account."""
        selected_row = self.options_list.currentRow()
        selected = (
            self._saved_options[selected_row].credential_id
            if 0 <= selected_row < len(self._saved_options)
            else None
        )
        index = next(
            (
                i
                for i, option in enumerate(self._saved_options)
                if option.credential_id == credential_id
            ),
            None,
        )
        if index is None:
            return bool(self._saved_options)
        self._saved_options = tuple(
            option for option in self._saved_options if option.credential_id != credential_id
        )
        with QSignalBlocker(self.options_list):
            self.options_list.takeItem(index)
            row = next(
                (
                    i
                    for i, option in enumerate(self._saved_options)
                    if option.credential_id == selected
                ),
                len(self._saved_options),
            )
            self.options_list.setCurrentRow(row)
        self._handle_option_changed(row)
        return bool(self._saved_options)

    def confirm_login_removal(self, option: SavedSshLoginOption) -> bool:
        box = QMessageBox(self)
        box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        box.setObjectName("removeChooserSshLoginDialog")
        box.setWindowTitle(tr("Remove saved SSH login"))
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            tr(
                'Remove the saved password for "{p0}" on "{p1}"?',
                p0=option.username,
                p1=self.endpoint_label.text(),
            )
        )
        box.setInformativeText(
            tr(
                "This removes the saved login and password for all Termana profiles using this host and port. Server accounts, saved connection profiles and open terminals are unchanged. This cannot be undone."
            )
        )
        remove = QPushButton(tr("Remove login"), box)
        remove.setObjectName("confirmRemoveChooserSshLoginButton")
        remove.setProperty("actionRole", "danger")
        box.addButton(remove, QMessageBox.ButtonRole.DestructiveRole)
        cancel = QPushButton(tr("Cancel"), box)
        cancel.setObjectName("cancelRemoveChooserSshLoginButton")
        cancel.setProperty("actionRole", "secondary")
        box.addButton(cancel, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        self._removal_confirmation = box
        try:
            box.exec()
            return box.clickedButton() is remove
        finally:
            self._removal_confirmation = None
            schedule_delete_later(box)

    def _handle_option_changed(self, row: int) -> None:
        """Disable saved-login preferences for the terminal-entry choice."""
        uses_other_username = row == len(self._saved_options)
        if uses_other_username:
            if self.remember_checkbox.isEnabled():
                self._saved_remember_preference = self.remember_checkbox.isChecked()
            self.remember_checkbox.setEnabled(False)
            self.remember_checkbox.setChecked(False)
            self.remember_checkbox.setAccessibleDescription(
                tr("Another username can become available after its password is saved.")
            )
            self.connect_button.setText(tr("Continue in terminal"))
            self.connect_button.setAccessibleName(
                tr("Continue to enter another SSH username in the terminal")
            )
            self.connect_button.setToolTip(tr("Continue to the terminal's login as prompt"))
        else:
            self.remember_checkbox.setEnabled(True)
            self.remember_checkbox.setChecked(self._saved_remember_preference)
            self.remember_checkbox.setAccessibleDescription(
                tr("The login chooser will still appear, with this account selected first.")
            )
            if 0 <= row < len(self._saved_options):
                username = self._saved_options[row].username
                self.connect_button.setText(
                    tr("Connect as {p0}", p0=f"{_button_username(username)}")
                )
                self.connect_button.setAccessibleName(
                    tr("Connect using the saved SSH login {p0}", p0=f"{username}")
                )
                self.connect_button.setToolTip(
                    plain_text_tooltip(
                        tr("Connect as {p0} using the saved password", p0=f"{username}")
                    )
                )
            else:
                self.connect_button.setText(tr("Connect"))
                self.connect_button.setAccessibleName(tr("Connect with selected SSH login"))
                self.connect_button.setToolTip("")
        self.connect_button.setEnabled(row >= 0 and not self._removal_pending)
        self.remember_checkbox.setEnabled(not uses_other_username and not self._removal_pending)
        self.remove_button.setEnabled(
            0 <= row < len(self._saved_options) and not self._removal_pending
        )
        self._schedule_metrics_update()

    def eventFilter(self, watched, event: QEvent) -> bool:
        """Refresh text after native controls receive their final font and width."""
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ):
            self._schedule_metrics_update()
        return super().eventFilter(watched, event)

    def _schedule_metrics_update(self) -> None:
        if not self._metrics_update_pending:
            self._metrics_update_pending = True
            QTimer.singleShot(0, self, self._update_control_metrics)

    def _update_control_metrics(self) -> None:
        self._metrics_update_pending = False
        if not self.isVisible():
            return
        row_height = max(_OPTION_ROW_HEIGHT, self.options_list.fontMetrics().height() + 18)
        for index in range(self.options_list.count()):
            item = self.options_list.item(index)
            if item.sizeHint().height() != row_height:
                item.setSizeHint(QSize(0, row_height))
        visible_rows = min(self.options_list.count(), _MAX_VISIBLE_OPTION_ROWS)
        list_height = visible_rows * row_height + 2
        if self.options_list.height() != list_height:
            self.options_list.setFixedHeight(list_height)

        row = self.options_list.currentRow()
        saved_option = self._saved_options[row] if 0 <= row < len(self._saved_options) else None
        button = self.connect_button
        style_option = QStyleOptionButton()
        button.initStyleOption(style_option)
        contents = button.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, style_option, button
        )
        metrics = button.fontMetrics()
        prefix = tr("Connect as ")
        # Never size from the elided label: Connect -> Connect as ... -> Connect
        # otherwise feeds back through sizeHint and can oscillate every frame.
        caption = (
            prefix + _button_username(saved_option.username).replace("&&", "&")
            if saved_option is not None
            else tr("Continue in terminal")
            if row == len(self._saved_options)
            else tr("Connect")
        )
        horizontal_padding = max(0, button.width() - contents.width()) + 4
        footer_width = (
            self.width()
            - self.layout().contentsMargins().left()
            - self.layout().contentsMargins().right()
        )
        capacity = max(
            1, footer_width - self.cancel_button.sizeHint().width() - self.layout().spacing()
        )
        caption_width = (
            metrics.horizontalAdvance(prefix)
            + metrics.horizontalAdvance(_button_username(saved_option.username).replace("&&", "&"))
            if saved_option is not None
            else metrics.horizontalAdvance(caption)
        )
        stable_width = min(caption_width + horizontal_padding + 2, capacity)
        if button.width() != stable_width:
            button.setFixedWidth(stable_width)
            button.initStyleOption(style_option)
            contents = button.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, style_option, button
            )
        available = max(0, contents.width() - 4)
        username_width = available - metrics.horizontalAdvance(prefix)
        if saved_option is None:
            text = metrics.elidedText(caption, Qt.TextElideMode.ElideRight, available).replace(
                "&", "&&"
            )
        elif username_width < metrics.horizontalAdvance("…"):
            text = tr("Connect")
        else:
            username = metrics.elidedText(
                saved_option.username, Qt.TextElideMode.ElideMiddle, username_width
            )
            text = prefix + username.replace("&", "&&")
        if text != button.text():
            button.setText(text)

    def _remember_preference_changed(self, checked: bool) -> None:
        """Preserve a saved-login preference while the manual option is active."""
        if self.remember_checkbox.isEnabled():
            self._saved_remember_preference = checked
