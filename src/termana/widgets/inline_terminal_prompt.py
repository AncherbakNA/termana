"""Application-owned SSH username prompt rendered inside a terminal tab."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Final
from uuid import UUID, uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from termana.i18n import tr, validation_text
from termana.models.settings import TerminalFontSettings
from termana.models.ssh_credential import validate_ssh_username
from termana.terminal.font import resolve_terminal_font
from termana.theme import DEFAULT_THEME, TerminalColors

_MAX_INPUT_CHARACTERS: Final = 129
_MAX_ERROR_CHARACTERS: Final = 512


class InlineTerminalPromptPage(QFrame):
    """Collect one non-secret SSH username before any transport is launched."""

    submitted = Signal(str)
    cancel_requested = Signal()

    def __init__(
        self,
        terminal_name: str,
        *,
        terminal_colors: TerminalColors | None = None,
        terminal_font: TerminalFontSettings | None = None,
        session_id: UUID | None = None,
    ) -> None:
        """Build a terminal-like, process-free ``login as:`` input page."""
        super().__init__()

        if not isinstance(terminal_name, str):
            raise TypeError("terminal_name must be a string")
        if not terminal_name or any(
            unicodedata.category(character).startswith("C") for character in terminal_name
        ):
            raise ValueError("terminal_name must be non-empty display-safe text")
        if session_id is not None and not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID or None")

        self.setObjectName("inlineTerminalPromptPage")
        self.setAccessibleName(tr("SSH login for {p0}", p0=f"{terminal_name}"))
        self._session_id = uuid4() if session_id is None else session_id
        self._terminal_name = terminal_name
        self._submitted_username: str | None = None
        self._cancel_was_requested = False
        self._focus_guard: Callable[[], bool] = lambda: True
        self._terminal_colors = DEFAULT_THEME.terminal
        self._terminal_font_settings = TerminalFontSettings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)

        prompt_row = QHBoxLayout()
        prompt_row.setContentsMargins(0, 0, 0, 0)
        prompt_row.setSpacing(7)

        self.prompt_label = QLabel(tr("login as:"))
        self.prompt_label.setObjectName("inlineTerminalPromptLabel")
        self.prompt_label.setTextFormat(Qt.TextFormat.PlainText)

        self.username_input = QLineEdit()
        self.username_input.setObjectName("inlineTerminalUsernameInput")
        self.username_input.setAccessibleName(tr("SSH username"))
        self.username_input.setAccessibleDescription(
            tr("Enter the remote account name and press Return to connect.")
        )
        # One extra character preserves evidence of overflow instead of silently
        # truncating an invalid paste into a different, valid account name.
        self.username_input.setMaxLength(_MAX_INPUT_CHARACTERS)
        self.username_input.setFrame(False)
        self.username_input.setClearButtonEnabled(False)
        self.username_input.setInputMethodHints(
            Qt.InputMethodHint.ImhNoAutoUppercase | Qt.InputMethodHint.ImhNoPredictiveText
        )
        self.prompt_label.setBuddy(self.username_input)

        prompt_row.addWidget(self.prompt_label)
        prompt_row.addWidget(self.username_input, 1)
        layout.addLayout(prompt_row)

        self.error_label = QLabel()
        self.error_label.setObjectName("inlineTerminalPromptError")
        self.error_label.setAccessibleName(tr("SSH username validation error"))
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        layout.addStretch(1)

        self.setFocusProxy(self.username_input)
        self.username_input.returnPressed.connect(self._submit_username)

        self.escape_shortcut = self._cancel_shortcut(
            QKeySequence(Qt.Key.Key_Escape),
            "cancelInlineTerminalPromptEscapeShortcut",
        )
        self.control_c_shortcut = self._cancel_shortcut(
            QKeySequence("Ctrl+C"),
            "cancelInlineTerminalPromptControlCShortcut",
        )
        self.control_d_shortcut = self._cancel_shortcut(
            QKeySequence("Ctrl+D"),
            "cancelInlineTerminalPromptControlDShortcut",
        )

        self.set_terminal_colors(terminal_colors or DEFAULT_THEME.terminal)
        self.set_terminal_font(terminal_font or TerminalFontSettings())
        self.username_input.setFocus(Qt.FocusReason.OtherFocusReason)

    @property
    def session_id(self) -> UUID:
        """Return the immutable workspace identity reserved for this tab."""
        return self._session_id

    @property
    def terminal_name(self) -> str:
        """Return the immutable user-facing name reserved for this tab."""
        return self._terminal_name

    @property
    def submitted_username(self) -> str | None:
        """Return the frozen validated value, or ``None`` before submission."""
        return self._submitted_username

    @property
    def terminal_colors(self) -> TerminalColors:
        """Return the current terminal palette."""
        return self._terminal_colors

    @property
    def terminal_font_settings(self) -> TerminalFontSettings:
        """Return the current portable terminal font settings."""
        return self._terminal_font_settings

    def set_focus_guard(self, guard: Callable[[], bool]) -> None:
        """Let the owner decide whether asynchronous updates may take focus."""
        if not callable(guard):
            raise TypeError("guard must be callable")
        self._focus_guard = guard

    def set_terminal_colors(self, colors: TerminalColors) -> None:
        """Apply terminal colors without depending on the application stylesheet."""
        if not isinstance(colors, TerminalColors):
            raise TypeError("colors must be TerminalColors")
        self._terminal_colors = colors

        page_palette = self.palette()
        page_palette.setColor(QPalette.ColorRole.Window, QColor(colors.background))
        page_palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.foreground))
        self.setPalette(page_palette)
        self.setAutoFillBackground(True)

        label_palette = self.prompt_label.palette()
        label_palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.foreground))
        self.prompt_label.setPalette(label_palette)

        input_palette = self.username_input.palette()
        input_palette.setColor(QPalette.ColorRole.Base, QColor(colors.background))
        input_palette.setColor(QPalette.ColorRole.Text, QColor(colors.foreground))
        input_palette.setColor(QPalette.ColorRole.Highlight, QColor(colors.selection_background))
        input_palette.setColor(
            QPalette.ColorRole.HighlightedText,
            QColor(colors.selection_foreground),
        )
        self.username_input.setPalette(input_palette)
        self.username_input.setStyleSheet(
            "QLineEdit { background: transparent; border: none; padding: 0; }"
        )

        error_palette = self.error_label.palette()
        error_palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.ansi.red))
        self.error_label.setPalette(error_palette)

        self.set_terminal_font(self._terminal_font_settings)

    def set_terminal_font(self, settings: TerminalFontSettings) -> None:
        """Apply one resolved fixed-pitch font to the complete prompt row."""
        if not isinstance(settings, TerminalFontSettings):
            raise TypeError("settings must be TerminalFontSettings")
        self._terminal_font_settings = settings
        font = resolve_terminal_font(settings)
        self.setFont(font)
        self.prompt_label.setFont(font)
        self.username_input.setFont(font)
        self.error_label.setFont(font)

    def show_error(self, message: str) -> None:
        """Show a bounded plain-text error without interpreting markup or controls."""
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        safe_message = _display_safe_error(message)
        self.error_label.setText(safe_message)
        self.error_label.setVisible(bool(safe_message))
        if safe_message:
            self.error_label.setAccessibleName(
                tr("SSH username validation error: {p0}", p0=f"{safe_message}")
            )
        else:
            self.error_label.setAccessibleName(tr("SSH username validation error"))

    def reset_submission(self) -> None:
        """Return a submitted or cancelled page to its editable prompt state."""
        self._submitted_username = None
        self._cancel_was_requested = False
        self.show_error("")
        self.username_input.setEnabled(True)
        if self._focus_guard():
            self.username_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.username_input.selectAll()

    def _submit_username(self) -> None:
        """Validate and freeze exactly one submission before notifying the owner."""
        if self._submitted_username is not None or self._cancel_was_requested:
            return
        try:
            username = validate_ssh_username(self.username_input.text())
        except ValueError as error:
            self.show_error(validation_text(str(error)))
            self.username_input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        self._submitted_username = username
        self.show_error("")
        self.username_input.setEnabled(False)
        self.submitted.emit(username)

    def _request_cancel(self) -> None:
        """Freeze input and publish cancellation at most once until reset."""
        if self._submitted_username is not None or self._cancel_was_requested:
            return
        self._cancel_was_requested = True
        self.username_input.setEnabled(False)
        self.cancel_requested.emit()

    def _cancel_shortcut(self, sequence: QKeySequence, object_name: str) -> QShortcut:
        """Create one non-repeating page-scoped cancellation shortcut."""
        shortcut = QShortcut(sequence, self)
        shortcut.setObjectName(object_name)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.setAutoRepeat(False)
        shortcut.activated.connect(self._request_cancel)
        return shortcut


def _display_safe_error(message: str) -> str:
    """Bound local status text and neutralize terminal-active Unicode controls."""
    characters: list[str] = []
    for character in message[:_MAX_ERROR_CHARACTERS]:
        if character in "\r\n\t":
            characters.append(" ")
        elif unicodedata.category(character).startswith("C"):
            characters.append("\N{REPLACEMENT CHARACTER}")
        else:
            characters.append(character)
    return "".join(characters).strip()
