"""Secure terminal-styled secret input embedded in a running terminal page."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from termana.credentials.askpass_helper import MAX_PASSWORD_BYTES, MAX_PROMPT_BYTES
from termana.i18n import tr
from termana.models.settings import TerminalFontSettings
from termana.terminal.font import resolve_terminal_font
from termana.theme import DEFAULT_THEME, TerminalColors
from termana.ui_icons import interface_icon

_MAX_INPUT_CHARACTERS: Final = MAX_PASSWORD_BYTES + 1
_MAX_PROMPT_CHARACTERS: Final = MAX_PROMPT_BYTES
_MAX_ERROR_CHARACTERS: Final = 512
_DEFAULT_PROMPT: Final = "Password:"
_DEFAULT_RESPONSE_PROMPT: Final = "Response:"


class InlineTerminalSecretPrompt(QFrame):
    """Collect one SSH secret without opening a dialog or storing the value."""

    submitted = Signal(str)
    cancel_requested = Signal()
    geometry_changed = Signal()

    def __init__(
        self,
        *,
        terminal_colors: TerminalColors | None = None,
        terminal_font: TerminalFontSettings | None = None,
    ) -> None:
        """Build an initially hidden input row suitable for a ``TerminalPage``."""
        super().__init__()

        self.setObjectName("inlineTerminalSecretPrompt")
        self.setAccessibleName(tr("SSH secret prompt"))
        self._frozen = True
        self._submitted = False
        self._cancel_was_requested = False
        self._focus_guard: Callable[[], bool] = lambda: True
        self._is_host_password = True
        self._is_host_confirmation = False
        self._secret_label = tr("Password")
        self._terminal_colors = DEFAULT_THEME.terminal
        self._terminal_font_settings = TerminalFontSettings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)

        self.prompt_label = QLabel(tr(_DEFAULT_PROMPT))
        self.prompt_label.setObjectName("inlineTerminalSecretPromptLabel")
        self.prompt_label.setAccessibleName(tr("SSH password request"))
        self.prompt_label.setTextFormat(Qt.TextFormat.PlainText)
        self.prompt_label.setWordWrap(True)
        self.prompt_label.setMinimumWidth(0)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.prompt_label.setSizePolicy(policy)

        self.secret_input = QLineEdit()
        self.secret_input.setObjectName("inlineTerminalSecretInput")
        self.secret_input.setAccessibleName(tr("SSH password"))
        self.secret_input.setAccessibleDescription(
            tr("Enter the SSH password and press Return. Characters are shown as asterisks.")
        )
        self.secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        # One extra character lets validation reject an oversized paste instead
        # of accepting its silently truncated prefix as a different password.
        self.secret_input.setMaxLength(_MAX_INPUT_CHARACTERS)
        self.secret_input.setFrame(False)
        self.secret_input.setClearButtonEnabled(False)
        self.secret_input.setDragEnabled(False)
        self.secret_input.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.secret_input.setInputMethodHints(
            Qt.InputMethodHint.ImhHiddenText
            | Qt.InputMethodHint.ImhSensitiveData
            | Qt.InputMethodHint.ImhNoAutoUppercase
            | Qt.InputMethodHint.ImhNoPredictiveText
        )
        self.prompt_label.setBuddy(self.secret_input)

        self._prompt_row = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._prompt_row.setContentsMargins(0, 0, 0, 0)
        self._prompt_row.setSpacing(7)
        self._prompt_row.addWidget(self.prompt_label)
        self.answer_label = QLabel(tr("Answer (yes/no):"), self)
        self.answer_label.setTextFormat(Qt.TextFormat.PlainText)
        self.answer_label.setBuddy(self.secret_input)
        self.answer_label.hide()
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(7)
        input_row.addWidget(self.answer_label)
        input_row.addWidget(self.secret_input, 1)
        self._prompt_row.addLayout(input_row, 1)
        layout.addLayout(self._prompt_row)

        self.copy_key_details_button = QPushButton(tr("Copy key details"), self)
        self.copy_key_details_button.setObjectName("terminalCopyKeyDetailsButton")
        self.copy_key_details_button.setProperty("actionRole", "secondary")
        self.copy_key_details_button.setAutoDefault(False)
        self.copy_key_details_button.setDefault(False)
        self.copy_key_details_button.setAccessibleName(tr("Copy presented SSH key details"))
        self.copy_key_details_button.setToolTip(
            tr("Copy the displayed SSH key question for verification. Your response is not copied.")
        )
        self.copy_key_details_button.clicked.connect(self._copy_key_details)
        self.copy_key_details_button.setEnabled(False)
        self.copy_key_details_button.hide()
        layout.addWidget(self.copy_key_details_button, 0, Qt.AlignmentFlag.AlignRight)

        self.error_label = QLabel()
        self.error_label.setObjectName("inlineTerminalSecretPromptError")
        self.error_label.setAccessibleName(tr("SSH password validation error"))
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.setFocusProxy(self.secret_input)
        self.secret_input.returnPressed.connect(self._submit_secret)

        self.escape_shortcut = self._cancel_shortcut(
            QKeySequence(Qt.Key.Key_Escape),
            "cancelInlineTerminalSecretPromptEscapeShortcut",
        )
        self.control_c_shortcut = self._cancel_shortcut(
            QKeySequence("Ctrl+C"),
            "cancelInlineTerminalSecretPromptControlCShortcut",
        )
        self.control_d_shortcut = self._cancel_shortcut(
            QKeySequence("Ctrl+D"),
            "cancelInlineTerminalSecretPromptControlDShortcut",
        )

        self.set_terminal_colors(terminal_colors or DEFAULT_THEME.terminal)
        self.set_terminal_font(terminal_font or TerminalFontSettings())
        self.secret_input.setEnabled(False)
        self.hide()

    @property
    def is_frozen(self) -> bool:
        """Return whether the current request can no longer emit a result."""
        return self._frozen

    @property
    def was_submitted(self) -> bool:
        """Return whether the current request emitted a validated secret."""
        return self._submitted

    @property
    def cancel_was_requested(self) -> bool:
        """Return whether the current request emitted cancellation."""
        return self._cancel_was_requested

    @property
    def terminal_colors(self) -> TerminalColors:
        """Return the current terminal palette."""
        return self._terminal_colors

    @property
    def terminal_font_settings(self) -> TerminalFontSettings:
        """Return the current portable terminal font settings."""
        return self._terminal_font_settings

    def set_focus_guard(self, guard: Callable[[], bool]) -> None:
        """Let the owner decide whether asynchronous requests may take focus."""
        if not callable(guard):
            raise TypeError("guard must be callable")
        self._focus_guard = guard

    def begin(
        self,
        prompt: str,
        *,
        is_host_password: bool = True,
        is_host_confirmation: bool = False,
        restore_focus: bool = True,
    ) -> None:
        """Show a fresh editable request using bounded, display-safe prompt text."""
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        if not isinstance(is_host_password, bool):
            raise TypeError("is_host_password must be a boolean")
        if type(is_host_confirmation) is not bool or (is_host_password and is_host_confirmation):
            raise ValueError("Host confirmation must be a non-password request")
        if type(restore_focus) is not bool:
            raise TypeError("restore_focus must be a boolean")
        self.secret_input.setEchoMode(
            QLineEdit.EchoMode.Normal if is_host_confirmation else QLineEdit.EchoMode.Password
        )
        self._clear_secret()
        self._frozen = False
        self._submitted = False
        self._cancel_was_requested = False
        self._is_host_password = is_host_password
        self._is_host_confirmation = is_host_confirmation
        self.answer_label.setVisible(is_host_confirmation)
        self.copy_key_details_button.setVisible(is_host_confirmation)
        self.copy_key_details_button.setEnabled(is_host_confirmation)
        self._secret_label = tr("Password") if is_host_password else tr("Response")
        self.setAccessibleName(
            tr("SSH password prompt") if is_host_password else tr("SSH authentication prompt")
        )
        self.prompt_label.setAccessibleName(
            tr("SSH password request") if is_host_password else tr("OpenSSH authentication request")
        )
        self.secret_input.setAccessibleName(
            tr("SSH password") if is_host_password else tr("SSH authentication response")
        )
        self.secret_input.setAccessibleDescription(
            tr("Enter the SSH {p0} and press Return. ", p0=f"{self._secret_label.casefold()}")
            + (
                tr("Type yes or no.")
                if is_host_confirmation
                else tr("Characters are shown as asterisks; the secret is never shown.")
            )
        )
        fallback_prompt = tr(_DEFAULT_PROMPT) if is_host_password else tr(_DEFAULT_RESPONSE_PROMPT)
        self.prompt_label.setText(
            _display_safe_text(prompt, _MAX_PROMPT_CHARACTERS, multiline=True) or fallback_prompt
        )
        self.show_error("")
        self.secret_input.setReadOnly(False)
        self.secret_input.setEnabled(True)
        self.show()
        self.geometry_changed.emit()
        if restore_focus and self.isVisible() and self._focus_guard():
            self.secret_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def prepare_width(self, width: int) -> None:
        """Keep short requests/input on one terminal line; wrap longer questions."""
        text = self.prompt_label.text()
        metrics = self.prompt_label.fontMetrics()
        inline = (
            not self._is_host_confirmation
            and
            "\n" not in text
            and metrics.horizontalAdvance(text) + metrics.horizontalAdvance("M" * 8) + 7 < width
        )
        self._prompt_row.setDirection(
            QBoxLayout.Direction.LeftToRight if inline else QBoxLayout.Direction.TopToBottom
        )
        self.prompt_label.setMinimumWidth(metrics.horizontalAdvance(text) + 1 if inline else 0)
        self.prompt_label.setMaximumWidth(metrics.horizontalAdvance(text) + 1 if inline else 16777215)
        policy = QSizePolicy(
            QSizePolicy.Policy.Fixed if inline else QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        policy.setHeightForWidth(True)
        self.prompt_label.setSizePolicy(policy)
        self._prompt_row.invalidate()
        self.layout().activate()

    def reset_submission(self) -> None:
        """Clear and re-enable the current prompt after a rejected transition."""
        self._clear_secret()
        self._frozen = False
        self._submitted = False
        self._cancel_was_requested = False
        self.copy_key_details_button.setEnabled(self._is_host_confirmation)
        self.show_error("")
        self.secret_input.setEnabled(True)
        self.secret_input.setReadOnly(False)
        self.show()
        if self.isVisible() and self._focus_guard():
            self.secret_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _copy_key_details(self) -> None:
        """Copy only a live public identity question, never the response editor."""
        if not self._is_host_confirmation or self._frozen or not self.isVisible():
            return
        QGuiApplication.clipboard().setText(self.prompt_label.text())
        if self.copy_key_details_button.hasFocus() and self._focus_guard():
            self.secret_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def finish(self) -> None:
        """Clear all editable state, freeze the request, and hide the row."""
        self._clear_secret()
        self._frozen = True
        self._is_host_confirmation = False
        self.copy_key_details_button.setEnabled(False)
        self.copy_key_details_button.hide()
        self.secret_input.setEnabled(False)
        self.hide()

    def cancel(self) -> bool:
        """Cancel an active request and report whether a signal was emitted."""
        if self._frozen:
            return False
        self._request_cancel()
        return True

    def set_terminal_colors(self, colors: TerminalColors) -> None:
        """Apply terminal colors without depending on the application stylesheet."""
        if not isinstance(colors, TerminalColors):
            raise TypeError("colors must be TerminalColors")
        self._terminal_colors = colors
        self.copy_key_details_button.setIcon(interface_icon("copy", colors.foreground))

        # Application QWidget/QLabel rules otherwise replace these palettes at
        # first polish, reparenting or a later theme change. Keep the actual
        # painted prompt surface terminal-owned, not only its palette metadata.
        self.setStyleSheet(
            "QFrame#inlineTerminalSecretPrompt {"
            f" background-color: {colors.background}; color: {colors.foreground};"
            " } QLabel#inlineTerminalSecretPromptLabel {"
            f" background-color: transparent; color: {colors.foreground};"
            " } QLabel#inlineTerminalSecretPromptError {"
            f" background-color: transparent; color: {colors.ansi.red};"
            " }"
        )

        page_palette = self.palette()
        page_palette.setColor(QPalette.ColorRole.Window, QColor(colors.background))
        # QScrollArea.setWidget changes the child's background role to Base.
        page_palette.setColor(QPalette.ColorRole.Base, QColor(colors.background))
        page_palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.foreground))
        page_palette.setColor(QPalette.ColorRole.Text, QColor(colors.foreground))
        self.setPalette(page_palette)
        self.setAutoFillBackground(True)

        label_palette = self.prompt_label.palette()
        label_palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.foreground))
        label_palette.setColor(QPalette.ColorRole.Text, QColor(colors.foreground))
        self.prompt_label.setPalette(label_palette)

        input_palette = self.secret_input.palette()
        input_palette.setColor(QPalette.ColorRole.Base, QColor(colors.background))
        input_palette.setColor(QPalette.ColorRole.Text, QColor(colors.foreground))
        input_palette.setColor(QPalette.ColorRole.Highlight, QColor(colors.selection_background))
        input_palette.setColor(
            QPalette.ColorRole.HighlightedText,
            QColor(colors.selection_foreground),
        )
        self.secret_input.setPalette(input_palette)
        self.secret_input.setStyleSheet(
            "QLineEdit#inlineTerminalSecretInput {"
            f" background: transparent; color: {colors.foreground};"
            f" selection-background-color: {colors.selection_background};"
            f" selection-color: {colors.selection_foreground};"
            " lineedit-password-character: 42; min-height: 0; border: none; padding: 0; }"
        )

        error_palette = self.error_label.palette()
        error_palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.ansi.red))
        self.error_label.setPalette(error_palette)

    def set_terminal_font(self, settings: TerminalFontSettings) -> None:
        """Apply one resolved fixed-pitch font to the complete prompt row."""
        if not isinstance(settings, TerminalFontSettings):
            raise TypeError("settings must be TerminalFontSettings")
        self._terminal_font_settings = settings
        font = resolve_terminal_font(settings)
        self.setFont(font)
        self.prompt_label.setFont(font)
        self.secret_input.setFont(font)
        self.error_label.setFont(font)
        self.answer_label.setFont(font)
        self.geometry_changed.emit()

    def show_error(self, message: str) -> None:
        """Render a bounded plain-text error without terminal-active controls."""
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        safe_message = _display_safe_text(message, _MAX_ERROR_CHARACTERS)
        self.error_label.setText(safe_message)
        self.error_label.setVisible(bool(safe_message))
        accessible_prefix = tr("SSH {p0} validation error", p0=f"{self._secret_label.casefold()}")
        if safe_message:
            self.error_label.setAccessibleName(f"{accessible_prefix}: {safe_message}")
        else:
            self.error_label.setAccessibleName(accessible_prefix)
        self.geometry_changed.emit()

    def _submit_secret(self) -> None:
        """Validate, clear, and publish exactly one secret for this request."""
        if self._frozen:
            return
        secret = self.secret_input.text()
        error = _secret_validation_error(secret, self._secret_label)
        if (
            error is None
            and self._is_host_confirmation
            and (
                secret not in {"yes", "no"}
                and re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", secret) is None
            )
        ):
            error = tr("Type yes or no, or the exact displayed SHA256 fingerprint.")
        if error is not None:
            self._clear_secret()
            self.show_error(error)
            self.secret_input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        self._frozen = True
        self._submitted = True
        self.copy_key_details_button.setEnabled(False)
        self.show_error("")
        self._clear_secret()
        # Disabling the focused editor sends focus to a neighbouring native
        # widget on Windows. Keep an empty read-only focus anchor until the
        # exact request finishes; frozen state still rejects repeat submission.
        self.secret_input.setReadOnly(True)
        try:
            self.submitted.emit(secret)
        finally:
            secret = ""

    def _request_cancel(self) -> None:
        """Clear and publish cancellation at most once until reset or begin."""
        if self._frozen:
            return
        self._frozen = True
        self._cancel_was_requested = True
        self.copy_key_details_button.setEnabled(False)
        self._clear_secret()
        self.secret_input.setEnabled(False)
        self.cancel_requested.emit()

    def _clear_secret(self) -> None:
        """Remove the editable secret and any selection from the Qt object."""
        self.secret_input.clear()
        self.secret_input.deselect()

    def _cancel_shortcut(self, sequence: QKeySequence, object_name: str) -> QShortcut:
        """Create one non-repeating widget-scoped cancellation shortcut."""
        shortcut = QShortcut(sequence, self)
        shortcut.setObjectName(object_name)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.setAutoRepeat(False)
        shortcut.activated.connect(self._request_cancel)
        return shortcut


def _secret_validation_error(secret: str, label: str) -> str | None:
    """Return a fixed local error when a value cannot cross AskPass IPC."""
    if any(character in secret for character in "\x00\r\n"):
        return tr("{p0} cannot contain NUL or line breaks", p0=f"{label}")
    try:
        encoded_size = len(secret.encode("utf-8"))
    except UnicodeError:
        return tr("{p0} contains unsupported text", p0=f"{label}")
    if encoded_size > MAX_PASSWORD_BYTES:
        return tr("{p0} cannot exceed {p1} UTF-8 bytes", p0=f"{label}", p1=f"{MAX_PASSWORD_BYTES}")
    return None


def _display_safe_text(value: str, limit: int, *, multiline: bool = False) -> str:
    """Bound display text and neutralize controls, including bidi formatting."""
    characters: list[str] = []
    for character in value[:limit]:
        if multiline and character == "\n":
            characters.append("\n")
        elif character in "\r\n\t":
            characters.append(" ")
        elif unicodedata.category(character).startswith("C"):
            characters.append("\N{REPLACEMENT CHARACTER}")
        else:
            characters.append(character)
    return "".join(characters).strip()
