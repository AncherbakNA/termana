"""Compact, transport-agnostic controls for searching terminal history."""

from __future__ import annotations

from typing import Final, cast

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QResizeEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton

from termana.i18n import tr
from termana.terminal.search import MAX_SEARCH_QUERY_CHARACTERS

NARROW_SEARCH_BAR_WIDTH: Final = 298
FULL_SEARCH_BAR_WIDTH: Final = 438


class TerminalSearchBar(QFrame):
    """Collect a bounded search query and expose navigation intent as signals."""

    query_changed = Signal(str)
    options_changed = Signal()
    next_requested = Signal()
    previous_requested = Signal()
    close_requested = Signal()

    def __init__(self) -> None:
        """Build a hidden search bar without implementing any search policy."""
        super().__init__()
        self._layout_update_pending = False

        self.setObjectName("terminalSearchBar")
        self.setAccessibleName(tr("Find in terminal"))

        layout = QHBoxLayout(self)
        # This is an overlay: its owner chooses the available width. Let resize
        # hide optional controls before their minimum hints widen the whole bar.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self.query_input = QLineEdit()
        self.query_input.setObjectName("terminalSearchInput")
        self.query_input.setAccessibleName(tr("Terminal search query"))
        self.query_input.setAccessibleDescription(
            tr(
                "Enter text to find in the current terminal. Press Return for the next match or Shift plus Return for the previous match."
            )
        )
        self.query_input.setPlaceholderText(tr("Find in terminal"))
        self.query_input.setClearButtonEnabled(True)
        self.query_input.setMaxLength(MAX_SEARCH_QUERY_CHARACTERS)

        self.match_case_button = QPushButton(tr("Aa"))
        self.match_case_button.setObjectName("terminalSearchMatchCaseButton")
        self.match_case_button.setAccessibleName(tr("Match case"))
        self.match_case_button.setToolTip(tr("Match case: distinguish uppercase and lowercase"))
        self.match_case_button.setCheckable(True)

        self.whole_words_button = QPushButton(tr("W"))
        self.whole_words_button.setObjectName("terminalSearchWholeWordsButton")
        self.whole_words_button.setAccessibleName(tr("Whole words"))
        self.whole_words_button.setToolTip(
            tr("Whole words: find root, not rootfs or root_user. Punctuation separates words.")
        )
        self.whole_words_button.setCheckable(True)

        self.status_label = QLabel("0 / 0")
        self.status_label.setObjectName("terminalSearchStatus")
        self.status_label.setAccessibleName(tr("No terminal search matches"))
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.previous_button = QPushButton("↑")
        self.previous_button.setObjectName("terminalSearchPreviousButton")
        self.previous_button.setAccessibleName(tr("Previous terminal search match"))
        self.previous_button.setToolTip(tr("Previous match (Shift+Enter)"))

        self.next_button = QPushButton("↓")
        self.next_button.setObjectName("terminalSearchNextButton")
        self.next_button.setAccessibleName(tr("Next terminal search match"))
        self.next_button.setToolTip(tr("Next match (Enter)"))

        self.close_button = QPushButton("\N{MULTIPLICATION SIGN}")
        self.close_button.setObjectName("terminalSearchCloseButton")
        self.close_button.setAccessibleName(tr("Close terminal search"))
        self.close_button.setToolTip(tr("Close search (Escape)"))

        for button in (
            self.match_case_button,
            self.whole_words_button,
            self.previous_button,
            self.next_button,
            self.close_button,
        ):
            button.setFixedWidth(32)

        layout.addWidget(self.query_input, 1)
        layout.addWidget(self.match_case_button)
        layout.addWidget(self.whole_words_button)
        layout.addWidget(self.status_label)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.close_button)

        self.setFocusProxy(self.query_input)
        for control in (
            self.query_input,
            self.match_case_button,
            self.whole_words_button,
            self.previous_button,
            self.next_button,
            self.close_button,
        ):
            control.installEventFilter(self)
        self.query_input.textChanged.connect(self.query_changed.emit)
        self.match_case_button.toggled.connect(lambda _checked: self.options_changed.emit())
        self.whole_words_button.toggled.connect(lambda _checked: self.options_changed.emit())
        self.previous_button.clicked.connect(self._request_previous)
        self.next_button.clicked.connect(self._request_next)
        self.close_button.clicked.connect(self._request_close)

        self.reset_match_status()
        self.hide()

    def open(self) -> None:
        """Reveal the bar and select the existing query for quick replacement."""
        self.show()
        self.raise_()
        self.query_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.query_input.selectAll()

    def close(self) -> bool:
        """Hide the bar and discard its local query and result position."""
        self.query_input.clear()
        self.reset_match_status()
        return super().close()

    def set_match_status(
        self,
        current: int,
        total: int,
        *,
        truncated: bool = False,
    ) -> None:
        """Display a validated one-based match position supplied by the owner."""
        if isinstance(current, bool) or not isinstance(current, int):
            raise TypeError("current must be an integer")
        if isinstance(total, bool) or not isinstance(total, int):
            raise TypeError("total must be an integer")
        if current < 0 or total < 0:
            raise ValueError("match counts cannot be negative")
        if current > total:
            raise ValueError("current match cannot exceed total matches")
        if not isinstance(truncated, bool):
            raise TypeError("truncated must be a boolean")

        count_suffix = "+" if truncated else ""
        self.status_label.setText(f"{current} / {total}{count_suffix}")
        if total == 0:
            self.status_label.setAccessibleName(tr("No terminal search matches"))
        elif current == 0:
            qualifier = tr("at least ") if truncated else ""
            self.status_label.setAccessibleName(
                tr("{p0}{p1} terminal search matches", p0=f"{qualifier}", p1=f"{total}")
            )
        else:
            qualifier = tr("at least ") if truncated else ""
            self.status_label.setAccessibleName(
                tr(
                    "Terminal search match {p0} of {p1}{p2}",
                    p0=f"{current}",
                    p1=f"{qualifier}",
                    p2=f"{total}",
                )
            )
        self._refresh_status_tooltips()
        self._schedule_layout_update()

    def reset_match_status(self) -> None:
        """Restore the neutral result position used before a search runs."""
        self.set_match_status(0, 0)

    def set_search_pending(self, *, updating: bool = False) -> None:
        """Expose pending work without presenting a stale result count."""
        if not isinstance(updating, bool):
            raise TypeError("updating must be a boolean")
        state = tr("Updating") if updating else tr("Searching")
        self.status_label.setText(f"{state}\N{HORIZONTAL ELLIPSIS}")
        self.status_label.setAccessibleName(tr("Terminal search {p0}", p0=f"{state.lower()}"))
        self._refresh_status_tooltips()
        self._schedule_layout_update()

    def _refresh_status_tooltips(self) -> None:
        summary = self.status_label.accessibleName()
        self.status_label.setToolTip(summary)
        self.query_input.setToolTip(summary)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Map Return navigation keys while leaving text editing untouched."""
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._schedule_layout_update()
        if event.type() is QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if (
                key_event.key() == Qt.Key.Key_Escape
                and key_event.modifiers() == Qt.KeyboardModifier.NoModifier
            ):
                self._request_close()
                key_event.accept()
                return True
            if (
                watched is self.query_input
                and key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and key_event.modifiers()
                in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier)
            ):
                if key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._request_previous()
                else:
                    self._request_next()
                key_event.accept()
                return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the query usable when the terminal pane becomes very narrow."""
        super().resizeEvent(event)
        self._adapt_controls()

    def _schedule_layout_update(self) -> None:
        if not self._layout_update_pending:
            self._layout_update_pending = True
            QTimer.singleShot(0, self, self._adapt_controls)

    def _adapt_controls(self) -> None:
        self._layout_update_pending = False
        buttons = (
            self.match_case_button,
            self.whole_words_button,
            self.previous_button,
            self.next_button,
            self.close_button,
        )
        for button in buttons:
            width = max(32, button.fontMetrics().horizontalAdvance(button.text()) + 16)
            if button.width() != width:
                button.setFixedWidth(width)
        extra_width = sum(button.width() - 32 for button in buttons)
        extra_width += max(0, self.status_label.sizeHint().width() - 80)
        show_navigation = self.width() >= FULL_SEARCH_BAR_WIDTH + extra_width
        show_status = self.width() >= NARROW_SEARCH_BAR_WIDTH + extra_width
        # Hiding a focused control moves Qt focus before hasFocus can be queried.
        restore_focus = not show_navigation and (
            self.previous_button.hasFocus() or self.next_button.hasFocus()
        )
        self.previous_button.setVisible(show_navigation)
        self.next_button.setVisible(show_navigation)
        self.status_label.setVisible(show_status)
        if restore_focus:
            self.query_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _request_next(self) -> None:
        """Publish forward navigation without deciding how matches are found."""
        self.next_requested.emit()

    def _request_previous(self) -> None:
        """Publish backward navigation without deciding how matches are found."""
        self.previous_requested.emit()

    def _request_close(self) -> None:
        """Clear and hide the bar before notifying its owner of user intent."""
        self.close()
        self.close_requested.emit()
