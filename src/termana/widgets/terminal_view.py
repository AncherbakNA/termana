"""Qt painting view for the fixed-size terminal screen model."""

from bisect import bisect_right
from collections.abc import Callable
from enum import Enum, auto
from math import ceil
from typing import Final

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAccessible,
    QAccessibleEvent,
    QAccessibleValueChangeEvent,
    QColor,
    QContextMenuEvent,
    QFocusEvent,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QInputMethodEvent,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QRegion,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QAbstractScrollArea, QAccessibleWidget, QFrame, QMenu

from termana.i18n import tr
from termana.models.settings import TerminalFontSettings
from termana.models.terminal_highlighting import LineHighlighter, TerminalHighlighting
from termana.qt_lifecycle import schedule_delete_later
from termana.terminal.font import resolve_terminal_font
from termana.terminal.input_encoder import TerminalInputEncoder
from termana.terminal.mouse import (
    MouseButton,
    MouseEventKind,
    MouseTrackingMode,
    encode_sgr_mouse_event,
)
from termana.terminal.screen import CellStyle, ScreenUpdate, TerminalCell, TerminalScreenModel
from termana.terminal.search import TerminalSearchMatch
from termana.terminal.selection import CellPosition, TerminalSelection, select_token
from termana.terminal.tab_shortcuts import terminal_tab_action_for_key_event
from termana.terminal.zoom import zoom_action_for_key_event
from termana.theme import DEFAULT_THEME, TerminalColors

HORIZONTAL_PADDING: Final = 12
VERTICAL_PADDING: Final = 10
DEFAULT_COLUMNS: Final = 80
DEFAULT_ROWS: Final = 24
MAX_RESOLVED_STYLE_CACHE_ENTRIES: Final = 2048
MOUSE_WHEEL_DELTA_PER_STEP: Final = 120
MAX_MOUSE_WHEEL_REPORTS_PER_EVENT: Final = 32
ACCESSIBLE_VALUE_UPDATE_INTERVAL_MS: Final = 120

ANSI_COLOR_ATTRIBUTES: Final = {
    "black": "black",
    "red": "red",
    "green": "green",
    "brown": "yellow",
    "blue": "blue",
    "magenta": "magenta",
    "cyan": "cyan",
    "white": "white",
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightbrown": "bright_yellow",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    # pyte 0.8.2 emits this misspelling for SGR 105 backgrounds.
    "bfightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}


class _ClipboardAction(Enum):
    """Clipboard commands handled before terminal Control sequences."""

    COPY = auto()
    PASTE = auto()


class _ScrollbackAction(Enum):
    """Local navigation that must not be forwarded to the child process."""

    PAGE_UP = auto()
    PAGE_DOWN = auto()
    TOP = auto()
    BOTTOM = auto()


def resolve_terminal_color(
    value: str,
    palette: TerminalColors,
    *,
    default: str,
) -> QColor:
    """Resolve pyte color names and RGB strings through the selected theme."""
    if value == "default":
        return QColor(default)
    if len(value) == 6 and all(character in "0123456789abcdefABCDEF" for character in value):
        return QColor(f"#{value}")
    if attribute := ANSI_COLOR_ATTRIBUTES.get(value):
        return QColor(getattr(palette.ansi, attribute))
    return QColor(default)


class TerminalView(QAbstractScrollArea):
    """Paint terminal cells, ANSI colors, and the current block cursor."""

    grid_size_changed = Signal(int, int)
    document_changed = Signal(object, bool)
    viewport_resized = Signal()
    input_generated = Signal(str)
    user_key_generated = Signal(object)
    paste_requested = Signal(str)
    one_line_paste_requested = Signal(str)
    input_lock_requested = Signal(bool)
    find_requested = Signal()
    output_export_requested = Signal()
    saved_commands_requested = Signal()
    context_menu_closed = Signal()

    def __init__(
        self,
        screen: TerminalScreenModel,
        *,
        palette: TerminalColors | None = None,
        terminal_font: TerminalFontSettings | None = None,
        legacy_console_colors: bool = False,
    ) -> None:
        """Create a view over an injected terminal model."""
        super().__init__()
        if type(legacy_console_colors) is not bool:
            raise TypeError("legacy_console_colors must be a bool")
        self.setObjectName("terminalOutput")
        self.setAccessibleName(tr("Terminal output"))
        self.setAccessibleDescription(
            tr("Terminal screen output. Use the configured copy shortcuts after selecting text.")
        )
        self.screen = screen
        self._legacy_console_colors = legacy_console_colors
        self._highlighter = LineHighlighter()
        self._grid_resize_guard: Callable[[], bool] = lambda: True
        self._palette = palette or DEFAULT_THEME.terminal
        self._terminal_font_settings = (
            terminal_font if terminal_font is not None else TerminalFontSettings()
        )
        if not isinstance(self._terminal_font_settings, TerminalFontSettings):
            raise TypeError("terminal_font must be TerminalFontSettings or None")
        self._font_cache: dict[tuple[bool, bool, bool, bool], QFont] = {}
        self._resolved_style_cache: dict[
            tuple[CellStyle, bool],
            tuple[QColor, QColor, QFont],
        ] = {}
        self._cell_width = 1
        self._cell_height = 1
        self._font_height = 1.0
        self._font_ascent = 1.0
        self._refresh_palette_colors()
        self._input_encoder = TerminalInputEncoder()
        self._backspace_sends_ctrl_h = False
        self._word_delete_sends_ctrl_w = False
        self._input_enabled = False
        self._input_locked = False
        self._input_lock_available = False
        self._find_enabled = True
        self._output_export_enabled = True
        self._preedit_text = ""
        self._selection: TerminalSelection | None = None
        self._selection_covers_document = False
        self._selection_anchor: CellPosition | None = None
        self._token_selection_anchor: TerminalSelection | None = None
        self._search_matches: tuple[TerminalSearchMatch, ...] = ()
        self._active_search_match_index: int | None = None
        self._search_ranges_by_row: dict[int, tuple[tuple[int, int], ...]] = {}
        self._search_range_starts_by_row: dict[int, tuple[int, ...]] = {}
        self._primary_scroll_position: int | None = None
        self._primary_scroll_follow_bottom = True
        self._last_mouse_motion_cell: tuple[int, int] | None = None
        self._mouse_wheel_delta_y = 0
        self._mouse_wheel_pixel_delta_y = 0
        self._remote_mouse_buttons: dict[Qt.MouseButton, tuple[int, int]] = {}
        self._local_mouse_buttons: set[Qt.MouseButton] = set()
        self._right_mouse_gesture_remote: bool | None = None
        self._accessible_value_snapshot = ""
        self._accessible_update_timer = QTimer(self)
        self._accessible_update_timer.setSingleShot(True)
        self._accessible_update_timer.setInterval(ACCESSIBLE_VALUE_UPDATE_INTERVAL_MS)
        self._accessible_update_timer.timeout.connect(self._publish_accessible_value_change)

        self.setFont(resolve_terminal_font(self._terminal_font_settings))
        self._refresh_font_metrics()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.viewport().setMouseTracking(True)

        scroll_bar = self.verticalScrollBar()
        scroll_bar.setRange(0, self.screen.scrollback_size)
        scroll_bar.setPageStep(self.screen.rows)
        scroll_bar.setSingleStep(1)
        scroll_bar.setValue(scroll_bar.maximum())
        scroll_bar.valueChanged.connect(self._handle_scroll_value_changed)

    @property
    def input_enabled(self) -> bool:
        """Return whether keyboard events may reach the child process."""
        return self._input_enabled

    @property
    def terminal_colors(self) -> TerminalColors:
        """Return the immutable terminal palette currently being rendered."""
        return self._palette

    @property
    def terminal_font_settings(self) -> TerminalFontSettings:
        """Return the portable terminal font preference being rendered."""
        return self._terminal_font_settings

    def set_terminal_colors(self, colors: TerminalColors) -> None:
        """Recolor the current screen without changing its cells or selection."""
        if colors == self._palette:
            return
        self._palette = colors
        self._refresh_palette_colors()
        self._resolved_style_cache.clear()
        self.viewport().update()

    def set_legacy_console_colors(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("Console color mode must be boolean")
        self._legacy_console_colors = enabled
        self._resolved_style_cache.clear()
        self.viewport().update()

    def set_terminal_font(self, settings: TerminalFontSettings) -> None:
        """Apply a font and keep every visible or hidden grid in lockstep."""
        if not isinstance(settings, TerminalFontSettings):
            raise TypeError("settings must be TerminalFontSettings")
        font = resolve_terminal_font(settings)
        if settings == self._terminal_font_settings and self.font() == font:
            return
        self._terminal_font_settings = settings
        self.setFont(font)
        # FontChange synchronizes visible views. Hidden QTabWidget pages do not
        # receive a resize when selected later, so synchronize them explicitly.
        self.synchronize_grid_size()

    def set_input_enabled(self, enabled: bool) -> None:
        """Gate process input without visually disabling the terminal screen."""
        enabled = enabled and not self._input_locked
        if self._input_enabled == enabled:
            return
        damage = self._input_damage_rect()
        if not enabled:
            self._release_remote_mouse_buttons()
        self._input_enabled = enabled
        if not enabled:
            self._preedit_text = ""
            self._reset_mouse_reporting_transients()
            self._clear_mouse_gesture_ownership()
        self.viewport().update(damage.united(self._input_damage_rect()))
        self.updateMicroFocus()
        self._publish_accessible_status_change()

    def set_input_lock_state(self, locked: bool, *, available: bool = True) -> None:
        """Mirror the owning page's explicit policy in the context action."""
        self._input_locked = locked
        self._input_lock_available = available
        if locked:
            self.set_input_enabled(False)

    def set_highlighting(self, settings: TerminalHighlighting) -> None:
        if type(settings) is not TerminalHighlighting:
            raise TypeError("settings must be TerminalHighlighting")
        self._highlighter = LineHighlighter(settings)
        self.viewport().update()

    def set_find_enabled(self, enabled: bool) -> None:
        """Control whether a parent-owned terminal search may be requested."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        self._find_enabled = enabled

    @property
    def cell_width(self) -> int:
        """Return one character-cell width in device-independent pixels."""
        return self._cell_width

    @property
    def cell_height(self) -> int:
        """Return one character-cell height in device-independent pixels."""
        return self._cell_height

    @property
    def plain_text(self) -> str:
        """Expose the currently scrolled viewport without control sequences."""
        document_top = self.verticalScrollBar().value()
        lines = [
            self.screen.document_line_text(row).rstrip()
            for row in range(
                document_top,
                min(document_top + self.screen.rows, self.screen.document_rows),
            )
        ]
        return "\n".join(lines).rstrip("\n")

    @property
    def document_plain_text(self) -> str:
        """Expose retained terminal history and the live grid as plain text."""
        return self._complete_document_text()

    @property
    def accessible_status(self) -> str:
        """Describe input and viewport state without embedding terminal contents."""
        input_status = (
            tr("Input is enabled.") if self._input_enabled else tr("Input is unavailable.")
        )
        if self.screen.alternate_screen:
            viewport_status = tr("Showing the alternate terminal screen.")
        elif self.is_at_bottom:
            viewport_status = tr("Showing live terminal output.")
        else:
            first_line = self.verticalScrollBar().value() + 1
            last_line = min(
                self.verticalScrollBar().value() + self.screen.rows,
                self.screen.document_rows,
            )
            viewport_status = tr(
                "Showing scrollback lines {p0} through {p1} of {p2}.",
                p0=f"{first_line}",
                p1=f"{last_line}",
                p2=f"{self.screen.document_rows}",
            )
        return f"{input_status} {viewport_status}"

    @property
    def has_selection(self) -> bool:
        """Return whether the user selected at least one visible cell."""
        return self._selection is not None

    @property
    def is_at_bottom(self) -> bool:
        """Return whether the viewport follows the active terminal screen."""
        scroll_bar = self.verticalScrollBar()
        return scroll_bar.value() == scroll_bar.maximum()

    @property
    def selected_text(self) -> str:
        """Return selected terminal cells as clipboard-ready plain text."""
        if self._selection is None:
            return ""
        if self._selection_covers_document:
            return self._complete_document_text()
        return self._selection.extract_text(self.screen)

    @property
    def search_matches(self) -> tuple[TerminalSearchMatch, ...]:
        """Return the UI-only search ranges currently highlighted."""
        return self._search_matches

    @property
    def active_search_match_index(self) -> int | None:
        """Return the zero-based active result, if one is selected."""
        return self._active_search_match_index

    def feed(self, data: bytes) -> ScreenUpdate:
        """Feed backend bytes into the model and schedule changed rows."""
        was_at_bottom = self.is_at_bottom
        was_alternate = self.screen.alternate_screen
        old_mouse_generation = self.screen.mouse_protocol_generation
        old_reset_generation = self.screen.terminal_reset_generation
        old_scroll_position = self.verticalScrollBar().value()
        if data:
            self.clear_selection()
        update = self.screen.feed(data)
        self._synchronize_mouse_protocol_change(
            old_mouse_generation,
            old_reset_generation,
        )
        self._synchronize_scrollbar(update, follow_bottom=was_at_bottom)
        self._synchronize_buffer_transition(
            was_alternate=was_alternate,
            old_scroll_position=old_scroll_position,
            was_at_bottom=was_at_bottom,
            update=update,
        )
        if was_alternate != self.screen.alternate_screen:
            self._reset_mouse_reporting_transients()
        self.schedule_update(update)
        self._emit_document_changed(update, was_alternate=was_alternate)
        return update

    def feed_text(self, text: str) -> ScreenUpdate:
        """Feed application-owned status text into the same terminal grid."""
        was_at_bottom = self.is_at_bottom
        was_alternate = self.screen.alternate_screen
        old_mouse_generation = self.screen.mouse_protocol_generation
        old_reset_generation = self.screen.terminal_reset_generation
        old_scroll_position = self.verticalScrollBar().value()
        if text:
            self.clear_selection()
        update = self.screen.feed_text(text)
        self._synchronize_mouse_protocol_change(
            old_mouse_generation,
            old_reset_generation,
        )
        self._synchronize_scrollbar(update, follow_bottom=was_at_bottom)
        self._synchronize_buffer_transition(
            was_alternate=was_alternate,
            old_scroll_position=old_scroll_position,
            was_at_bottom=was_at_bottom,
            update=update,
        )
        if was_alternate != self.screen.alternate_screen:
            self._reset_mouse_reporting_transients()
        self.schedule_update(update)
        self._emit_document_changed(update, was_alternate=was_alternate)
        return update

    def finish(self) -> ScreenUpdate:
        """Flush the model decoder and schedule any final cells."""
        was_at_bottom = self.is_at_bottom
        was_alternate = self.screen.alternate_screen
        old_mouse_generation = self.screen.mouse_protocol_generation
        old_reset_generation = self.screen.terminal_reset_generation
        old_scroll_position = self.verticalScrollBar().value()
        update = self.screen.finish()
        self._synchronize_mouse_protocol_change(
            old_mouse_generation,
            old_reset_generation,
        )
        self._synchronize_scrollbar(update, follow_bottom=was_at_bottom)
        self._synchronize_buffer_transition(
            was_alternate=was_alternate,
            old_scroll_position=old_scroll_position,
            was_at_bottom=was_at_bottom,
            update=update,
        )
        if was_alternate != self.screen.alternate_screen:
            self._reset_mouse_reporting_transients()
        self.schedule_update(update)
        self._emit_document_changed(update, was_alternate=was_alternate)
        return update

    def set_grid_resize_guard(self, guard: Callable[[], bool]) -> None:
        """Let an owning layout defer transient geometry without touching terminal data."""
        if not callable(guard):
            raise TypeError("guard must be callable")
        self._grid_resize_guard = guard

    def synchronize_grid_size(self) -> None:
        """Resize model rows and columns to the current viewport."""
        if not self._grid_resize_guard():
            return
        available_width = max(0, self.viewport().width() - 2 * HORIZONTAL_PADDING)
        available_height = max(0, self.viewport().height() - 2 * VERTICAL_PADDING)
        columns = max(1, available_width // self.cell_width)
        rows = max(1, available_height // self.cell_height)
        if (columns, rows) == (self.screen.columns, self.screen.rows):
            return


        was_at_bottom = self.is_at_bottom
        was_alternate = self.screen.alternate_screen
        old_scroll_position = self.verticalScrollBar().value()
        self.clear_selection()
        update = self.screen.resize(columns, rows)
        self._clamp_remote_mouse_cells()
        self._reset_mouse_reporting_transients()
        self._synchronize_scrollbar(update, follow_bottom=was_at_bottom)
        self._synchronize_buffer_transition(
            was_alternate=was_alternate,
            old_scroll_position=old_scroll_position,
            was_at_bottom=was_at_bottom,
            update=update,
        )
        self.schedule_update(update)
        self._emit_document_changed(
            update,
            was_alternate=was_alternate,
            force_full_invalidation=True,
        )
        self.grid_size_changed.emit(columns, rows)

    def scroll_to_bottom(self) -> None:
        """Return from history to the live screen without changing its contents."""
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def scroll_to_document_row(self, row: int) -> None:
        """Reveal one absolute document row without changing terminal contents."""
        if isinstance(row, bool) or not isinstance(row, int):
            raise TypeError("row must be an integer")
        if not 0 <= row < self.screen.document_rows:
            raise ValueError("row must identify the current terminal document")

        scroll_bar = self.verticalScrollBar()
        top = scroll_bar.value()
        bottom = top + max(1, self.screen.rows) - 1
        if top <= row <= bottom:
            return
        centered_top = row - self.screen.rows // 2
        scroll_bar.setValue(min(max(centered_top, scroll_bar.minimum()), scroll_bar.maximum()))

    def set_search_matches(
        self,
        matches: tuple[TerminalSearchMatch, ...],
        active_index: int | None,
    ) -> None:
        """Replace UI-only literal-search highlights and repaint the viewport."""
        if not isinstance(matches, tuple) or any(
            not isinstance(match, TerminalSearchMatch) for match in matches
        ):
            raise TypeError("matches must be a tuple of TerminalSearchMatch values")
        if active_index is not None and (
            isinstance(active_index, bool)
            or not isinstance(active_index, int)
            or not 0 <= active_index < len(matches)
        ):
            raise ValueError("active_index must identify a supplied match or be None")

        ranges_by_row: dict[int, list[tuple[int, int]]] = {}
        for match in matches:
            if (
                match.row < 0
                or match.row >= self.screen.document_rows
                or match.start_column < 0
                or match.end_column < match.start_column
                or match.end_column >= self.screen.columns
            ):
                raise ValueError("search matches must use valid document coordinates")
            ranges_by_row.setdefault(match.row, []).append((match.start_column, match.end_column))

        self._search_matches = matches
        self._active_search_match_index = active_index
        self._search_ranges_by_row = {
            row: tuple(sorted(ranges)) for row, ranges in ranges_by_row.items()
        }
        self._search_range_starts_by_row = {
            row: tuple(start for start, _end in ranges)
            for row, ranges in self._search_ranges_by_row.items()
        }
        self.viewport().update()

    def clear_search_matches(self) -> None:
        """Discard search highlights without moving the viewport or selection."""
        if not self._search_matches:
            return
        self._search_matches = ()
        self._active_search_match_index = None
        self._search_ranges_by_row.clear()
        self._search_range_starts_by_row.clear()
        self.viewport().update()

    def _synchronize_scrollbar(
        self,
        update: ScreenUpdate,
        *,
        follow_bottom: bool,
    ) -> None:
        """Update range while preserving either live-follow or the viewed line."""
        scroll_bar = self.verticalScrollBar()
        old_value = scroll_bar.value()
        if follow_bottom:
            new_value = self.screen.scrollback_size
        else:
            new_value = max(0, old_value - update.scrollback_lines_dropped)

        blocker = QSignalBlocker(scroll_bar)
        scroll_bar.setRange(0, self.screen.scrollback_size)
        scroll_bar.setPageStep(self.screen.rows)
        scroll_bar.setValue(new_value)
        del blocker

        if update.scrollback_cleared or (
            not follow_bottom and update.scrollback_lines_dropped > old_value
        ):
            self.viewport().update()

    def _synchronize_buffer_transition(
        self,
        *,
        was_alternate: bool,
        old_scroll_position: int,
        was_at_bottom: bool,
        update: ScreenUpdate,
    ) -> None:
        """Preserve the primary viewport while an alternate buffer is visible."""
        is_alternate = self.screen.alternate_screen
        if not was_alternate and is_alternate:
            self._primary_scroll_position = max(
                0,
                old_scroll_position - update.scrollback_lines_dropped,
            )
            self._primary_scroll_follow_bottom = was_at_bottom
            self.viewport().update()
            self.updateMicroFocus()
            return

        if was_alternate and is_alternate:
            if self._primary_scroll_position is not None:
                self._primary_scroll_position = max(
                    0,
                    self._primary_scroll_position - update.scrollback_lines_dropped,
                )
            return

        if not was_alternate or self._primary_scroll_position is None:
            return

        scroll_bar = self.verticalScrollBar()
        restored_value = (
            scroll_bar.maximum()
            if self._primary_scroll_follow_bottom
            else max(
                0,
                self._primary_scroll_position - update.scrollback_lines_dropped,
            )
        )
        blocker = QSignalBlocker(scroll_bar)
        scroll_bar.setValue(min(restored_value, scroll_bar.maximum()))
        del blocker
        self._primary_scroll_position = None
        self._primary_scroll_follow_bottom = True
        self.viewport().update()
        self.updateMicroFocus()

    def _handle_scroll_value_changed(self, _value: int) -> None:
        """Repaint document coordinates after wheel, drag, or key navigation."""
        self._reset_mouse_reporting_transients()
        self.viewport().update()
        self.updateMicroFocus()
        self._schedule_accessible_value_change()

    def schedule_update(self, update: ScreenUpdate) -> None:
        """Ask Qt to repaint only rows reported as damaged by the model."""
        valid_rows = {row for row in update.dirty_rows if 0 <= row < self.screen.rows}
        if not valid_rows:
            return
        region = QRegion()
        document_top = self.verticalScrollBar().value()
        live_screen_start = self.screen.scrollback_size
        for row in valid_rows:
            viewport_row = live_screen_start + row - document_top
            if not 0 <= viewport_row < self.screen.rows:
                continue
            top = VERTICAL_PADDING + viewport_row * self.cell_height
            region += QRect(0, top, self.viewport().width(), self.cell_height)
        if not region.isEmpty():
            self.viewport().update(region)
        self.updateMicroFocus()

    def _emit_document_changed(
        self,
        update: ScreenUpdate,
        *,
        was_alternate: bool,
        force_full_invalidation: bool = False,
    ) -> None:
        """Publish screen damage plus whether absolute search ranges became unsafe."""
        full_invalidation = (
            force_full_invalidation
            or was_alternate != self.screen.alternate_screen
            or update.scrollback_cleared
            or update.scrollback_lines_dropped > 0
        )
        self.document_changed.emit(update, full_invalidation)
        self._schedule_accessible_value_change()

    def cell_rect(self, row: int, column: int) -> QRect:
        """Return a cell rectangle for painting and stable geometry tests."""
        return QRect(
            HORIZONTAL_PADDING + column * self.cell_width,
            VERTICAL_PADDING + row * self.cell_height,
            self.cell_width,
            self.cell_height,
        )

    def sizeHint(self) -> QSize:
        """Suggest a familiar 80-by-24 terminal size to layouts."""
        return QSize(
            DEFAULT_COLUMNS * self.cell_width
            + 2 * HORIZONTAL_PADDING
            + self.verticalScrollBar().sizeHint().width(),
            DEFAULT_ROWS * self.cell_height + 2 * VERTICAL_PADDING,
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        """Paint cells inside the exposed viewport region."""
        painter = QPainter(self.viewport())
        painter.setClipRegion(event.region())
        painter.fillRect(event.rect(), self._background_color)

        document_top = self.verticalScrollBar().value()
        for viewport_row, first_column, last_column in self._exposed_rows(event.region()):
            document_row = document_top + viewport_row
            if document_row >= self.screen.document_rows:
                continue
            cells = self.screen.document_row_cells(document_row)
            self._paint_row_backgrounds(
                painter,
                viewport_row,
                document_row,
                first_column,
                last_column,
                cells,
            )
            self._paint_row_text(
                painter,
                viewport_row,
                document_row,
                first_column,
                last_column,
                cells,
            )
        self._paint_cursor(painter, event.rect())
        self._paint_preedit(painter, event.rect())

    def event(self, event: QEvent) -> bool:
        """Capture terminal shortcuts and Tab before Qt changes focus."""
        if isinstance(event, QKeyEvent):
            is_find_shortcut = event.matches(QKeySequence.StandardKey.Find)
            if event.type() == QEvent.Type.ShortcutOverride and is_find_shortcut:
                # Let a containing TerminalPage reveal its search bar. The
                # matching KeyPress fallback below keeps this local command out
                # of the PTY when a bare view has no parent shortcut.
                event.ignore()
                return False
            if event.type() == QEvent.Type.KeyPress and is_find_shortcut:
                event.accept()
                return True
            terminal_tab_action = terminal_tab_action_for_key_event(event)
            if event.type() == QEvent.Type.ShortcutOverride and terminal_tab_action is not None:
                event.ignore()
                return False
            if event.type() == QEvent.Type.KeyPress and terminal_tab_action is not None:
                # A parent QAction normally consumes this event. Keep it out of
                # the PTY when a bare view is embedded without those actions.
                event.accept()
                return True
            zoom_action = zoom_action_for_key_event(event)
            if event.type() == QEvent.Type.ShortcutOverride and zoom_action is not None:
                event.ignore()
                return False
            if event.type() == QEvent.Type.KeyPress and zoom_action is not None:
                # A parent QAction normally consumes this event. Keep it out of
                # the PTY even when the view is embedded without those actions.
                event.accept()
                return True
            clipboard_action = self._clipboard_action(event)
            scrollback_action = self._scrollback_action(event)
            if event.type() == QEvent.Type.ShortcutOverride:
                if (
                    clipboard_action is not None
                    or scrollback_action is not None
                    or (self._input_enabled and self._encoded_key_event(event) is not None)
                ):
                    event.accept()
                    return True
            if event.type() == QEvent.Type.KeyPress and scrollback_action is not None:
                self._perform_scrollback_action(scrollback_action)
                event.accept()
                return True
            if event.type() == QEvent.Type.KeyPress and clipboard_action is not None:
                if not event.isAutoRepeat():
                    self._perform_clipboard_action(clipboard_action)
                event.accept()
                return True
            if (
                event.type() == QEvent.Type.KeyPress
                and event.key()
                in {
                    Qt.Key.Key_Tab,
                    Qt.Key.Key_Backtab,
                }
                and self._input_enabled
            ):
                if self._emit_key_event(event):
                    return True
        return super().event(event)

    def viewportEvent(self, event: QEvent) -> bool:
        """Keep right-click gestures out of selection and default press handlers."""
        if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.RightButton:
            if event.type() == QEvent.Type.MouseButtonPress:
                if self._can_report_mouse_at(event.position(), event.modifiers()):
                    return super().viewportEvent(event)
                self._remember_local_mouse_press(event.button())
                self._right_mouse_gesture_remote = False
                event.accept()
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                if event.button() in self._remote_mouse_buttons:
                    return super().viewportEvent(event)
                self._local_mouse_buttons.discard(event.button())
                event.accept()
                return True
        return super().viewportEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Translate one Qt key press into terminal input."""
        if self._input_enabled and self._emit_key_event(event):
            return
        super().keyPressEvent(event)

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        """Send committed IME text while keeping composition outside the model."""
        if not self._input_enabled:
            event.ignore()
            return

        damage = self._input_damage_rect()
        self._preedit_text = event.preeditString()
        if committed_text := event.commitString():
            self.scroll_to_bottom()
            self.clear_selection()
            self.input_generated.emit(committed_text)
        self.viewport().update(damage.united(self._input_damage_rect()))
        self.updateMicroFocus()
        event.accept()

    def inputMethodQuery(self, query: Qt.InputMethodQuery) -> object:
        """Tell platform input methods where the terminal cursor is located."""
        if query == Qt.InputMethodQuery.ImEnabled:
            return self._input_enabled
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            rectangle = self._cursor_rect()
            rectangle.translate(self.viewport().pos())
            return rectangle
        if query == Qt.InputMethodQuery.ImFont:
            return self.font()
        return super().inputMethodQuery(query)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the model grid aligned with the viewport size."""
        super().resizeEvent(event)
        self.synchronize_grid_size()
        self.viewport_resized.emit()

    def changeEvent(self, event: QEvent) -> None:
        """Refresh cached geometry when Qt changes the effective font or screen."""
        super().changeEvent(event)
        if event.type() not in {
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
            QEvent.Type.ScreenChangeInternal,
            QEvent.Type.DevicePixelRatioChange,
        }:
            return
        self._refresh_font_metrics()
        self._reset_mouse_reporting_transients()
        self.updateGeometry()
        self.viewport().update()
        if self.isVisible():
            self.synchronize_grid_size()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Start a linear selection and focus the clicked terminal."""
        if self._begin_remote_mouse_press(event):
            return
        self._remember_local_mouse_press(event.button())
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.clear_selection()
            self._selection_anchor = self._cell_position_from_point(event.position())
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            # Avoid selection/default press handling. A containing page can
            # restore a protected focus owner after the popup closes.
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Select a local token, preserving double-click reports to remote apps."""
        self.mousePressEvent(event)
        if event.button() != Qt.MouseButton.LeftButton or self._selection_anchor is None:
            return
        selection = select_token(self.screen, self._selection_anchor)
        self._token_selection_anchor = selection
        self._selection = selection
        self._update_selection_rows(None, selection)

    def _drag_selection_to(self, head: CellPosition) -> TerminalSelection:
        """Extend a normal drag by cells or a double-click drag by whole tokens."""
        token = self._token_selection_anchor
        if token is not None:
            target = select_token(self.screen, head)
            if head < token.start:
                return TerminalSelection(token.end, target.start)
            return TerminalSelection(token.start, max(token.end, target.end))
        assert self._selection_anchor is not None
        return TerminalSelection(self._selection_anchor, head)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Extend the active selection while the left button remains held."""
        if self._emit_mouse_motion_event(event):
            return
        if self._selection_anchor is not None and event.buttons() & Qt.MouseButton.LeftButton:
            head = self._cell_position_from_point(event.position())
            selection = (
                None
                if head == self._selection_anchor and self._token_selection_anchor is None
                else self._drag_selection_to(head)
            )
            if selection != self._selection:
                old_selection = self._selection
                self._selection = selection
                self._selection_covers_document = False
                self._update_selection_rows(old_selection, selection)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Finish a drag selection without forwarding it to the scroll area."""
        if self._finish_remote_mouse_release(event):
            return
        self._local_mouse_buttons.discard(event.button())
        if event.button() == Qt.MouseButton.LeftButton and self._selection_anchor is not None:
            head = self._cell_position_from_point(event.position())
            selection = (
                None
                if head == self._selection_anchor and self._token_selection_anchor is None
                else self._drag_selection_to(head)
            )
            old_selection = self._selection
            self._selection = selection
            self._selection_covers_document = False
            self._selection_anchor = None
            self._token_selection_anchor = None
            self._update_selection_rows(old_selection, selection)
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Report vertical wheel steps to a child or keep local scrollback behavior."""
        cell = self._reported_mouse_cell(event.position(), event.modifiers())
        if cell is None:
            self._reset_mouse_wheel_accumulators()
            super().wheelEvent(event)
            return

        angle_delta_y = event.angleDelta().y()
        pixel_delta_y = event.pixelDelta().y()
        if angle_delta_y:
            self._mouse_wheel_pixel_delta_y = 0
            self._mouse_wheel_delta_y += angle_delta_y
            step_count, self._mouse_wheel_delta_y = self._bounded_mouse_wheel_steps(
                self._mouse_wheel_delta_y,
                MOUSE_WHEEL_DELTA_PER_STEP,
            )
        elif pixel_delta_y:
            self._mouse_wheel_delta_y = 0
            self._mouse_wheel_pixel_delta_y += pixel_delta_y
            step_count, self._mouse_wheel_pixel_delta_y = self._bounded_mouse_wheel_steps(
                self._mouse_wheel_pixel_delta_y,
                max(1, self.cell_height),
            )
        else:
            self._reset_mouse_wheel_accumulators()
            super().wheelEvent(event)
            return

        if step_count:
            self.clear_selection()
            button = MouseButton.WHEEL_UP if step_count > 0 else MouseButton.WHEEL_DOWN
            for _ in range(abs(step_count)):
                self._emit_sgr_mouse_report(
                    button,
                    cell,
                    MouseEventKind.PRESS,
                    event.modifiers(),
                )
        event.accept()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """Offer local terminal commands without mutating terminal selection."""
        if event.reason() is QContextMenuEvent.Reason.Mouse:
            remote_gesture = self._right_mouse_gesture_remote
            self._right_mouse_gesture_remote = None
            if remote_gesture is True or (
                remote_gesture is None
                and self._can_report_mouse_at(QPointF(event.pos()), event.modifiers())
            ):
                event.accept()
                return
        menu = self._create_context_menu()
        selected_action = None
        try:
            selected_action = menu.exec(event.globalPos())
        finally:
            schedule_delete_later(menu)
            self.context_menu_closed.emit()
        if selected_action is not None and selected_action.objectName() == "terminalFindAction":
            # QMenu restores the previous focus owner as exec() returns. Open
            # Find only after that restoration has completed.
            self._request_find()
        elif (
            selected_action is not None
            and selected_action.objectName() == "terminalSaveOutputAction"
        ):
            self._request_output_export()
        elif (
            selected_action is not None
            and selected_action.objectName() == "terminalSavedCommandsAction"
            and self._input_enabled
        ):
            self.saved_commands_requested.emit()
        event.accept()

    def focusInEvent(self, event: QFocusEvent) -> None:
        """Repaint the cursor as an active block."""
        super().focusInEvent(event)
        # Focus itself causes assistive technology to query the current value.
        # Starting from that value prevents an immediate duplicate announcement.
        self._accessible_value_snapshot = self.plain_text
        self.viewport().update(self._input_damage_rect())

    def focusOutEvent(self, event: QFocusEvent) -> None:
        """Repaint the cursor as an inactive outline."""
        super().focusOutEvent(event)
        self._accessible_update_timer.stop()
        self.viewport().update(self._input_damage_rect())

    def copy_selection(self) -> bool:
        """Copy selected visible cells without clearing an existing clipboard."""
        if self._selection is None:
            return False
        QGuiApplication.clipboard().setText(self.selected_text)
        return True

    def paste_from_clipboard(self) -> bool:
        """Snapshot clipboard text during a user action and request a paste."""
        return self._paste_from_clipboard(one_line=False)

    def paste_as_one_line_from_clipboard(self) -> bool:
        """Explicitly request line-break-to-space conversion without Enter."""
        return self._paste_from_clipboard(one_line=True)

    def _paste_from_clipboard(self, *, one_line: bool) -> bool:
        if not self._input_enabled:
            return False
        mime_data = QGuiApplication.clipboard().mimeData()
        if mime_data is None or not mime_data.hasText():
            return False
        text = mime_data.text()
        if not text:
            return False
        signal = self.one_line_paste_requested if one_line else self.paste_requested
        signal.emit(text)
        return True

    def select_all(self) -> bool:
        """Select the complete current document, including retained history."""
        if self.screen.document_rows <= 0 or self.screen.columns <= 0:
            return False
        selection = TerminalSelection(
            CellPosition(0, 0),
            CellPosition(self.screen.document_rows - 1, self.screen.columns - 1),
        )
        old_selection = self._selection
        self._selection = selection
        self._selection_covers_document = True
        self._selection_anchor = None
        self._token_selection_anchor = None
        self._update_selection_rows(old_selection, selection)
        return True

    def clear_selection(self) -> None:
        """Clear UI-only selection state and repaint its previous rows."""
        selection = self._selection
        self._selection = None
        self._selection_covers_document = False
        self._selection_anchor = None
        self._token_selection_anchor = None
        if selection is None:
            return
        self._update_selection_rows(selection, None)

    def set_output_export_enabled(self, enabled: bool) -> None:
        """Allow local output export only while the page's lifecycle permits it."""
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        self._output_export_enabled = enabled

    def _request_output_export(self) -> None:
        """Recheck a stale context-menu action before asking to export output."""
        if self._output_export_enabled:
            self.output_export_requested.emit()

    def _create_context_menu(self) -> QMenu:
        """Build one short-lived menu with state captured at opening time."""
        menu = QMenu(self)
        menu.setObjectName("terminalContextMenu")

        copy_action = menu.addAction(tr("Copy"))
        copy_action.setObjectName("terminalCopyAction")
        copy_action.setEnabled(self.has_selection)
        copy_action.triggered.connect(lambda _checked=False: self.copy_selection())

        paste_action = menu.addAction(tr("Paste"))
        paste_action.setObjectName("terminalPasteAction")
        paste_action.setEnabled(self._can_paste_from_clipboard())
        paste_action.triggered.connect(lambda _checked=False: self.paste_from_clipboard())
        one_line_action = menu.addAction(tr("Paste as one line"))
        one_line_action.setObjectName("terminalPasteOneLineAction")
        one_line_action.setToolTip(tr("Replace line breaks with spaces; never append Enter"))
        one_line_action.setEnabled(self._can_paste_from_clipboard())
        one_line_action.triggered.connect(
            lambda _checked=False: self.paste_as_one_line_from_clipboard()
        )
        commands_action = menu.addAction(tr("Saved commands…"))
        commands_action.setObjectName("terminalSavedCommandsAction")
        commands_action.setEnabled(self._input_enabled)

        select_all_action = menu.addAction(tr("Select all"))
        select_all_action.setObjectName("terminalSelectAllAction")
        select_all_action.setEnabled(self.screen.document_rows > 0 and self.screen.columns > 0)
        select_all_action.triggered.connect(lambda _checked=False: self.select_all())

        menu.addSeparator()
        find_action = menu.addAction(tr("Find"))
        find_action.setObjectName("terminalFindAction")
        find_action.setEnabled(self._find_enabled)
        export_action = menu.addAction(tr("Save output…"))
        export_action.setObjectName("terminalSaveOutputAction")
        export_action.setToolTip(
            tr("Save retained output as UTF-8 text; it may contain sensitive data")
        )
        export_action.setEnabled(self._output_export_enabled)
        if self._input_lock_available:
            menu.addSeparator()
            lock_action = menu.addAction(tr("Read-only input"))
            lock_action.setObjectName("terminalInputLockAction")
            lock_action.setToolTip(
                tr(
                    "Lock this session's terminal input. Copy and Find remain available; SSH authentication is separate. New sessions start unlocked."
                )
            )
            lock_action.setCheckable(True)
            lock_action.setChecked(self._input_locked)
            lock_action.triggered.connect(self.input_lock_requested)
        return menu

    def _can_paste_from_clipboard(self) -> bool:
        """Check text availability without materializing the clipboard payload."""
        if not self._input_enabled:
            return False
        mime_data = QGuiApplication.clipboard().mimeData()
        return bool(mime_data is not None and mime_data.hasText())

    def _request_find(self) -> None:
        """Emit Find only while the current owner still permits terminal search."""
        if self._find_enabled:
            self.find_requested.emit()

    def _complete_document_text(self) -> str:
        """Extract all physical rows without materializing styled scrollback cells."""
        lines = [
            self.screen.document_line_text(row).rstrip(" ")
            for row in range(self.screen.document_rows)
        ]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def _schedule_accessible_value_change(self) -> None:
        """Bound focused viewport updates without starving continuous output."""
        if (
            self._accessible_update_timer.isActive()
            or not self.hasFocus()
            or not QAccessible.isActive()
        ):
            return
        # Do not restart this timer for later chunks: one event is guaranteed
        # after the bounded interval even if the PTY continuously produces data.
        self._accessible_update_timer.start()

    def _publish_accessible_value_change(self) -> None:
        """Publish one current viewport value without changing the focus name."""
        if not self.hasFocus() or not QAccessible.isActive():
            return
        value = self.plain_text
        if value == self._accessible_value_snapshot:
            return
        self._accessible_value_snapshot = value
        QAccessible.updateAccessibility(QAccessibleValueChangeEvent(self, value))

    def _publish_accessible_status_change(self) -> None:
        """Notify a focused assistive client when terminal input availability changes."""
        if not self.hasFocus() or not QAccessible.isActive():
            return
        QAccessible.updateAccessibility(
            QAccessibleEvent(self, QAccessible.Event.DescriptionChanged)
        )

    def _paint_row_backgrounds(
        self,
        painter: QPainter,
        viewport_row: int,
        document_row: int,
        first_column: int,
        last_column: int,
        cells: tuple[TerminalCell, ...],
    ) -> None:
        """Paint adjacent equal backgrounds as one rectangle."""
        if (
            self._selection is None
            and not self._search_ranges_by_row.get(document_row)
            and all(
                cell.style.background == "default" and not cell.style.reverse
                for cell in cells[first_column : last_column + 1]
            )
        ):
            # paintEvent already fills the exposed region with the terminal's
            # default background.  Most output changes only foreground text;
            # avoid resolving the same default style for every visible cell.
            return

        run_start: int | None = None
        run_color: QColor | None = None

        for column in range(first_column, last_column + 2):
            background = self._background_color
            if column <= last_column:
                _foreground, background, _font = self._resolved_cell_style(
                    document_row,
                    column,
                    cells[column],
                )

            if background == self._background_color:
                if run_start is not None and run_color is not None:
                    self._fill_background_run(
                        painter,
                        viewport_row,
                        run_start,
                        column,
                        run_color,
                    )
                    run_start = None
                    run_color = None
                continue

            if run_start is None:
                run_start = column
                run_color = background
            elif background != run_color:
                self._fill_background_run(
                    painter,
                    viewport_row,
                    run_start,
                    column,
                    run_color,
                )
                run_start = column
                run_color = background

    def _paint_row_text(
        self,
        painter: QPainter,
        viewport_row: int,
        document_row: int,
        first_column: int,
        last_column: int,
        cells: tuple[TerminalCell, ...],
    ) -> None:
        """Paint visible glyphs after the row's backgrounds are complete."""
        baseline = self._baseline_for_row(viewport_row)
        # This state lives for one row paint only: theme/font/selection changes
        # cannot leave stale cached colors or retain output between frames.
        has_overlays = self._selection is not None or bool(
            self._search_ranges_by_row.get(document_row)
        )
        previous_style = None
        resolved = None
        painted_font = None
        painted_foreground = None
        highlights = {}
        if not self.screen.alternate_screen and self._highlighter.settings.enabled:
            text = "".join(cell.text for cell in cells)
            ranges = self._highlighter.ranges(text)
            offsets = []
            for index, cell in enumerate(cells):
                offsets.extend([index] * len(cell.text))
            colors = {}
            for start, end, color in ranges:
                tone = colors.setdefault(color, QColor(color))
                for index in set(offsets[start:end]):
                    highlights.setdefault(index, tone)
        for column in range(first_column, last_column + 1):
            cell = cells[column]
            if not cell.text or (
                cell.text == " " and not cell.style.underline and not cell.style.strikethrough
            ):
                continue

            if has_overlays:
                foreground, _background, font = self._resolved_cell_style(
                    document_row, column, cell
                )
            else:
                if resolved is None or cell.style != previous_style:
                    resolved = self._resolved_style(cell.style)
                    previous_style = cell.style
                foreground, _background, font = resolved
            if (
                column in highlights
                and cell.style.foreground == "default"
                and cell.style.background == "default"
                and not cell.style.reverse
                and not self._is_cell_selected(document_row, column, cell)
                and not self._search_state(document_row, column)
            ):
                foreground = highlights[column]
            if font is not painted_font:
                painter.setFont(font)
                painted_font = font
            if foreground is not painted_foreground:
                painter.setPen(foreground)
                painted_foreground = foreground
            painter.drawText(
                QPointF(HORIZONTAL_PADDING + column * self.cell_width, baseline),
                cell.text,
            )

    def _fill_background_run(
        self,
        painter: QPainter,
        viewport_row: int,
        first_column: int,
        end_column: int,
        color: QColor,
    ) -> None:
        """Fill a half-open horizontal range of terminal cells."""
        painter.fillRect(
            QRect(
                HORIZONTAL_PADDING + first_column * self.cell_width,
                VERTICAL_PADDING + viewport_row * self.cell_height,
                (end_column - first_column) * self.cell_width,
                self.cell_height,
            ),
            color,
        )

    def _paint_cursor(self, painter: QPainter, clip: QRect) -> None:
        """Draw a block cursor last so its location remains obvious."""
        cursor = self.screen.cursor
        if (
            cursor.hidden
            or not self.is_at_bottom
            or self.screen.rows < 1
            or self.screen.columns < 1
        ):
            return

        row = min(max(cursor.row, 0), self.screen.rows - 1)
        column = min(max(cursor.column, 0), self.screen.columns - 1)
        rectangle = self.cell_rect(row, column)
        if not rectangle.intersects(clip):
            return

        cell = self.screen.cell(row, column)
        if self._input_enabled and self.hasFocus():
            painter.fillRect(rectangle, self._cursor_color)
        else:
            painter.setPen(self._cursor_color)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Keep the whole one-logical-pixel stroke inside the damaged cell.
            # An integer edge spills half a pixel above it on Retina displays,
            # leaving a line behind when only the next dirty row is repainted.
            painter.drawRect(QRectF(rectangle).adjusted(0.5, 0.5, -0.5, -0.5))
            return

        if cell.text and cell.text != " ":
            painter.setFont(self._font_for_cell(cell))
            painter.setPen(self._cursor_text_color)
            painter.drawText(
                QPointF(rectangle.left(), self._baseline_for_row(row)),
                cell.text,
            )

    def _paint_preedit(self, painter: QPainter, clip: QRect) -> None:
        """Show an IME composition without writing it into the terminal grid."""
        if (
            not self._preedit_text
            or not self._input_enabled
            or not self.hasFocus()
            or not self.is_at_bottom
        ):
            return
        rectangle = self._cursor_rect()
        if not self._input_damage_rect().intersects(clip):
            return

        font = QFont(self.font())
        font.setUnderline(True)
        painter.setFont(font)
        painter.setPen(self._foreground_color)
        painter.drawText(
            QPointF(rectangle.left(), self._baseline_for_row(self.screen.cursor.row)),
            self._preedit_text,
        )

    def _encoded_key_event(self, event: QKeyEvent) -> str | None:
        """Encode a key unless it belongs to tab navigation between sessions."""
        if terminal_tab_action_for_key_event(event) is not None:
            return None
        return self._input_encoder.encode(
            event.key(),
            event.text(),
            event.modifiers(),
            application_cursor_keys=self.screen.application_cursor_keys,
            shell_word_navigation=(
                not self.screen.alternate_screen and self.screen.mouse_tracking_mode is None
            ),
            backspace_sends_ctrl_h=self._backspace_sends_ctrl_h,
            word_delete_sends_ctrl_w=self._word_delete_sends_ctrl_w,
        )

    def set_word_delete_sends_ctrl_w(self, enabled: bool) -> None:
        """Select one connection's backward-word mapping, never plain Backspace."""
        if type(enabled) is not bool:
            raise TypeError("Word deletion preference must be boolean")
        self._word_delete_sends_ctrl_w = enabled

    def set_backspace_sends_ctrl_h(self, enabled: bool) -> None:
        """Change only plain Backspace encoding; preserve word deletion and auth editors."""
        if type(enabled) is not bool:
            raise TypeError("Backspace preference must be boolean")
        self._backspace_sends_ctrl_h = enabled

    def _begin_remote_mouse_press(self, event: QMouseEvent) -> bool:
        """Latch one button to the child when its press begins inside the live grid."""
        terminal_button = self._terminal_mouse_button(event.button())
        cell = self._reported_mouse_cell(event.position(), event.modifiers())
        if terminal_button is None or cell is None:
            return False
        self._local_mouse_buttons.discard(event.button())
        self._remote_mouse_buttons[event.button()] = cell
        if event.button() == Qt.MouseButton.RightButton:
            self._right_mouse_gesture_remote = True
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.clear_selection()
        self._last_mouse_motion_cell = None
        self._emit_sgr_mouse_report(
            terminal_button,
            cell,
            MouseEventKind.PRESS,
            event.modifiers(),
        )
        event.accept()
        return True

    def _finish_remote_mouse_release(self, event: QMouseEvent) -> bool:
        """Close only a child-owned press, retaining its last valid cell if needed."""
        last_cell = self._remote_mouse_buttons.pop(event.button(), None)
        if last_cell is None:
            return False
        terminal_button = self._terminal_mouse_button(event.button())
        if terminal_button is not None and self._mouse_transport_available():
            current_cell = (
                self._live_grid_cell_from_point(event.position()) if self.is_at_bottom else None
            )
            self._emit_sgr_mouse_report(
                terminal_button,
                current_cell or last_cell,
                MouseEventKind.RELEASE,
                event.modifiers(),
            )
        self._last_mouse_motion_cell = None
        event.accept()
        return True

    def _emit_mouse_motion_event(self, event: QMouseEvent) -> bool:
        """Emit deduplicated cell motion for button-event or any-event mode."""
        owned_button = self._pressed_remote_mouse_button(event.buttons())
        if owned_button is not None:
            qt_button, terminal_button = owned_button
            mode = self.screen.mouse_tracking_mode
            if (
                self._mouse_transport_available()
                and self.is_at_bottom
                and mode in {MouseTrackingMode.BUTTON_EVENT, MouseTrackingMode.ANY_EVENT}
            ):
                cell = self._live_grid_cell_from_point(event.position())
                if cell is not None and cell != self._last_mouse_motion_cell:
                    self._emit_sgr_mouse_report(
                        terminal_button,
                        cell,
                        MouseEventKind.MOTION,
                        event.modifiers(),
                    )
                    self._remote_mouse_buttons[qt_button] = cell
                    self._last_mouse_motion_cell = cell
            event.accept()
            return True

        if self._pressed_terminal_mouse_button(event.buttons()) is not None:
            return False

        cell = self._reported_mouse_cell(event.position(), event.modifiers())
        mode = self.screen.mouse_tracking_mode
        if cell is None or mode is not MouseTrackingMode.ANY_EVENT:
            return False
        if cell != self._last_mouse_motion_cell:
            self._emit_sgr_mouse_report(
                MouseButton.NONE,
                cell,
                MouseEventKind.MOTION,
                event.modifiers(),
            )
            self._last_mouse_motion_cell = cell
        event.accept()
        return True

    def _emit_sgr_mouse_report(
        self,
        button: MouseButton,
        cell: tuple[int, int],
        kind: MouseEventKind,
        modifiers: Qt.KeyboardModifier,
    ) -> None:
        """Encode one ASCII-only SGR report through the existing input boundary."""
        column, row = cell
        self.input_generated.emit(
            encode_sgr_mouse_event(
                button,
                column,
                row,
                kind,
                shift=bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
                alt=bool(modifiers & Qt.KeyboardModifier.AltModifier),
                control=bool(modifiers & self._input_encoder.terminal_control_modifier),
            )
        )

    def _reported_mouse_cell(
        self,
        point: QPointF,
        modifiers: Qt.KeyboardModifier,
    ) -> tuple[int, int] | None:
        """Return one-based live-grid coordinates only while reporting is active."""
        if not self._mouse_reporting_active(modifiers):
            return None
        return self._live_grid_cell_from_point(point)

    def _can_report_mouse_at(
        self,
        point: QPointF,
        modifiers: Qt.KeyboardModifier,
    ) -> bool:
        """Return whether this exact pointer event belongs to the child process."""
        return self._reported_mouse_cell(point, modifiers) is not None

    def _mouse_reporting_active(self, modifiers: Qt.KeyboardModifier) -> bool:
        """Apply input, mode, history, and local-override gates in one place."""
        if not self._mouse_transport_available() or not self.is_at_bottom:
            return False
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return False
        return not bool(modifiers & self._input_encoder.application_modifier)

    def _mouse_transport_available(self) -> bool:
        """Return whether ASCII SGR reports may safely reach the running child."""
        return bool(
            self._input_enabled
            and self.screen.mouse_tracking_mode is not None
            and self.screen.sgr_mouse_reporting
        )

    def _live_grid_cell_from_point(self, point: QPointF) -> tuple[int, int] | None:
        """Map a point to a real live cell without clamping or wide-cell rewinding."""
        x = point.x() - HORIZONTAL_PADDING
        y = point.y() - VERTICAL_PADDING
        grid_width = self.screen.columns * self.cell_width
        grid_height = self.screen.rows * self.cell_height
        if not (0 <= x < grid_width and 0 <= y < grid_height):
            return None
        return int(x // self.cell_width) + 1, int(y // self.cell_height) + 1

    @staticmethod
    def _terminal_mouse_button(button: Qt.MouseButton) -> MouseButton | None:
        """Map one Qt button identity to xterm's ordinary button values."""
        return {
            Qt.MouseButton.LeftButton: MouseButton.LEFT,
            Qt.MouseButton.MiddleButton: MouseButton.MIDDLE,
            Qt.MouseButton.RightButton: MouseButton.RIGHT,
        }.get(button)

    @staticmethod
    def _pressed_terminal_mouse_button(
        buttons: Qt.MouseButton,
    ) -> MouseButton | None:
        """Choose a stable xterm identity if one or more buttons are held."""
        for qt_button, terminal_button in (
            (Qt.MouseButton.LeftButton, MouseButton.LEFT),
            (Qt.MouseButton.MiddleButton, MouseButton.MIDDLE),
            (Qt.MouseButton.RightButton, MouseButton.RIGHT),
        ):
            if buttons & qt_button:
                return terminal_button
        return None

    def _pressed_remote_mouse_button(
        self,
        buttons: Qt.MouseButton,
    ) -> tuple[Qt.MouseButton, MouseButton] | None:
        """Return the first held button whose press was reported to the child."""
        for qt_button in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.MiddleButton,
            Qt.MouseButton.RightButton,
        ):
            if buttons & qt_button and qt_button in self._remote_mouse_buttons:
                terminal_button = self._terminal_mouse_button(qt_button)
                if terminal_button is not None:
                    return qt_button, terminal_button
        return None

    def _remember_local_mouse_press(self, button: Qt.MouseButton) -> None:
        """Latch a non-reported ordinary press so its release cannot become remote."""
        if self._terminal_mouse_button(button) is None:
            return
        self._remote_mouse_buttons.pop(button, None)
        self._local_mouse_buttons.add(button)
        if button == Qt.MouseButton.RightButton:
            self._right_mouse_gesture_remote = False

    def _release_remote_mouse_buttons(self) -> None:
        """Send safe releases before an input gate can strand child-owned buttons."""
        if self._mouse_transport_available():
            for qt_button, cell in tuple(self._remote_mouse_buttons.items()):
                terminal_button = self._terminal_mouse_button(qt_button)
                if terminal_button is not None:
                    self._emit_sgr_mouse_report(
                        terminal_button,
                        cell,
                        MouseEventKind.RELEASE,
                        Qt.KeyboardModifier.NoModifier,
                    )
        self._remote_mouse_buttons.clear()

    def _synchronize_mouse_protocol_change(
        self,
        old_mouse_generation: int,
        old_reset_generation: int,
    ) -> None:
        """Reset protocol transients without breaking a still-compatible gesture."""
        if old_mouse_generation == self.screen.mouse_protocol_generation:
            return
        if (
            old_reset_generation != self.screen.terminal_reset_generation
            or not self._mouse_transport_available()
        ):
            self._clear_remote_mouse_ownership()
        self._reset_mouse_reporting_transients()

    def _clear_remote_mouse_ownership(self) -> None:
        """Forget presses after a reset or incompatible mouse-protocol change."""
        self._remote_mouse_buttons.clear()
        if self._right_mouse_gesture_remote is True:
            self._right_mouse_gesture_remote = None

    def _clear_mouse_gesture_ownership(self) -> None:
        """Forget all latched pointer ownership after terminal input is disabled."""
        self._remote_mouse_buttons.clear()
        self._local_mouse_buttons.clear()
        self._right_mouse_gesture_remote = None

    def _clamp_remote_mouse_cells(self) -> None:
        """Keep release coordinates valid if the grid shrinks during a gesture."""
        self._remote_mouse_buttons = {
            button: (
                min(max(column, 1), self.screen.columns),
                min(max(row, 1), self.screen.rows),
            )
            for button, (column, row) in self._remote_mouse_buttons.items()
        }

    @staticmethod
    def _bounded_mouse_wheel_steps(total: int, quantum: int) -> tuple[int, int]:
        """Bound reports per event, discard excess backlog, and retain one remainder."""
        raw_steps = int(total / quantum)
        remainder = total - raw_steps * quantum
        bounded_steps = min(
            max(raw_steps, -MAX_MOUSE_WHEEL_REPORTS_PER_EVENT),
            MAX_MOUSE_WHEEL_REPORTS_PER_EVENT,
        )
        return bounded_steps, remainder

    def _reset_mouse_wheel_accumulators(self) -> None:
        """Discard partial wheel gestures when local UI regains ownership."""
        self._mouse_wheel_delta_y = 0
        self._mouse_wheel_pixel_delta_y = 0

    def _reset_mouse_reporting_transients(self) -> None:
        """Forget motion dedupe and partial wheel input across ownership changes."""
        self._last_mouse_motion_cell = None
        self._reset_mouse_wheel_accumulators()

    def _emit_key_event(self, event: QKeyEvent) -> bool:
        """Emit one handled terminal sequence and preserve focus."""
        sequence = self._encoded_key_event(event)
        if sequence is None:
            return False
        damage = self._input_damage_rect()
        self._preedit_text = ""
        self.scroll_to_bottom()
        self.clear_selection()
        self.input_generated.emit(sequence)
        self.user_key_generated.emit((event.key(), event.text(), event.modifiers()))
        event.accept()
        self.viewport().update(damage.united(self._input_damage_rect()))
        return True

    def _input_damage_rect(self) -> QRect:
        """Cover the whole composition row, including a previously painted tail."""
        rectangle = self._cursor_rect()
        if self._preedit_text:
            rectangle.setLeft(0)
            rectangle.setRight(self.viewport().width() - 1)
        return rectangle

    def _cursor_rect(self) -> QRect:
        """Return the current cursor cell, clamped to the visible grid."""
        cursor = self.screen.cursor
        document_row = self.screen.scrollback_size + cursor.row
        row = document_row - self.verticalScrollBar().value()
        column = min(max(cursor.column, 0), self.screen.columns - 1)
        return self.cell_rect(row, column)

    def _resolved_cell_style(
        self,
        row: int,
        column: int,
        cell: TerminalCell,
    ) -> tuple[QColor, QColor, QFont]:
        """Resolve cached colors/font and apply UI-only selection colors."""
        foreground, background, font = self._resolved_style(cell.style)
        if self._is_cell_selected(row, column, cell):
            return (
                self._selection_foreground_color,
                self._selection_background_color,
                font,
            )
        search_state = self._search_state(row, column)
        if search_state == 2:
            return self._cursor_text_color, self._cursor_color, font
        if search_state == 1:
            return (
                self._selection_foreground_color,
                self._selection_background_color,
                font,
            )
        return foreground, background, font

    def _resolved_style(self, style: CellStyle) -> tuple[QColor, QColor, QFont]:
        """Resolve each immutable terminal style once across repaint passes."""
        console_base = self._legacy_console_colors and not self.screen.alternate_screen and not style.reverse
        cache_key = (style, console_base)
        cached_style = self._resolved_style_cache.get(cache_key)
        if cached_style is not None:
            return cached_style

        foreground = resolve_terminal_color(
            style.foreground,
            self._palette,
            default=self._palette.foreground,
        )
        background = resolve_terminal_color(
            style.background,
            self._palette,
            default=self._palette.background,
        )
        # Network consoles commonly keep SGR40/37 for ordinary text instead of
        # resetting to39/49. Present that base pair in the active theme. Retain
        # the original parser attributes, colored backgrounds, literal RGB and
        # alternate-screen/reverse-video semantics; SSH/local are unchanged.
        if console_base and style.background == "black":
            background = self._background_color
            if style.foreground == "white":
                foreground = QColor(self._palette.foreground)
        if style.reverse:
            foreground, background = background, foreground
        resolved_style = (foreground, background, self._font_for_style(style))
        if len(self._resolved_style_cache) >= MAX_RESOLVED_STYLE_CACHE_ENTRIES:
            self._resolved_style_cache.clear()
        self._resolved_style_cache[cache_key] = resolved_style
        return resolved_style

    def _is_cell_selected(self, row: int, column: int, cell: TerminalCell) -> bool:
        """Include both cells occupied by a selected wide character."""
        if self._selection is None:
            return False
        if self._selection.contains(row, column):
            return True
        return column > 0 and cell.text == "" and self._selection.contains(row, column - 1)

    def _search_state(self, row: int, column: int) -> int:
        """Return 0 for none, 1 for a match, or 2 for the active match."""
        active_index = self._active_search_match_index
        if active_index is not None:
            active = self._search_matches[active_index]
            if active.row == row and active.start_column <= column <= active.end_column:
                return 2

        ranges = self._search_ranges_by_row.get(row)
        starts = self._search_range_starts_by_row.get(row)
        if not ranges or not starts:
            return 0
        range_index = bisect_right(starts, column) - 1
        if range_index >= 0 and column <= ranges[range_index][1]:
            return 1
        return 0

    def _cell_position_from_point(self, point: QPointF) -> CellPosition:
        """Convert viewport pixels to a clamped, wide-character-safe cell."""
        column = int((point.x() - HORIZONTAL_PADDING) // self.cell_width)
        viewport_row = int((point.y() - VERTICAL_PADDING) // self.cell_height)
        column = min(max(column, 0), self.screen.columns - 1)
        viewport_row = min(max(viewport_row, 0), self.screen.rows - 1)
        document_row = min(
            self.verticalScrollBar().value() + viewport_row,
            self.screen.document_rows - 1,
        )
        if column > 0 and self.screen.document_cell(document_row, column).text == "":
            column -= 1
        return CellPosition(document_row, column)

    def _clipboard_action(self, event: QKeyEvent) -> _ClipboardAction | None:
        """Recognize platform copy/paste shortcuts before terminal Ctrl keys."""
        modifiers = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        application_modifier = self._input_encoder.application_modifier
        terminal_control = self._input_encoder.terminal_control_modifier

        if self._input_encoder.is_macos:
            if event.key() == Qt.Key.Key_C and modifiers == application_modifier:
                return _ClipboardAction.COPY
            if event.key() == Qt.Key.Key_V and modifiers == application_modifier:
                return _ClipboardAction.PASTE
        else:
            if event.key() == Qt.Key.Key_C and modifiers == (
                terminal_control | Qt.KeyboardModifier.ShiftModifier
            ):
                return _ClipboardAction.COPY
            if event.key() == Qt.Key.Key_V and modifiers == (
                terminal_control | Qt.KeyboardModifier.ShiftModifier
            ):
                return _ClipboardAction.PASTE
            if event.key() == Qt.Key.Key_Insert and modifiers == terminal_control:
                return _ClipboardAction.COPY

        if event.key() == Qt.Key.Key_Insert and modifiers == Qt.KeyboardModifier.ShiftModifier:
            return _ClipboardAction.PASTE
        return None

    @staticmethod
    def _scrollback_action(event: QKeyEvent) -> _ScrollbackAction | None:
        """Map Shift navigation keys to local history movement."""
        if event.modifiers() != Qt.KeyboardModifier.ShiftModifier:
            return None
        return {
            Qt.Key.Key_PageUp: _ScrollbackAction.PAGE_UP,
            Qt.Key.Key_PageDown: _ScrollbackAction.PAGE_DOWN,
            Qt.Key.Key_Home: _ScrollbackAction.TOP,
            Qt.Key.Key_End: _ScrollbackAction.BOTTOM,
        }.get(event.key())

    def _perform_clipboard_action(self, action: _ClipboardAction) -> None:
        """Execute a recognized copy or paste user gesture."""
        if action is _ClipboardAction.COPY:
            self.copy_selection()
        else:
            self.paste_from_clipboard()

    def _perform_scrollback_action(self, action: _ScrollbackAction) -> None:
        """Move the local scrollbar without generating terminal bytes."""
        scroll_bar = self.verticalScrollBar()
        page = max(1, scroll_bar.pageStep() - 1)
        if action is _ScrollbackAction.PAGE_UP:
            scroll_bar.setValue(scroll_bar.value() - page)
        elif action is _ScrollbackAction.PAGE_DOWN:
            scroll_bar.setValue(scroll_bar.value() + page)
        elif action is _ScrollbackAction.TOP:
            scroll_bar.setValue(scroll_bar.minimum())
        else:
            scroll_bar.setValue(scroll_bar.maximum())

    def _font_for_cell(self, cell: TerminalCell) -> QFont:
        """Return the cached font variant for an immutable cell snapshot."""
        return self._font_for_style(cell.style)

    def _font_for_style(self, style: CellStyle) -> QFont:
        """Reuse the small set of font variants requested by ANSI styles."""
        key = (
            style.bold,
            style.italic,
            style.underline,
            style.strikethrough,
        )
        if key not in self._font_cache:
            font = QFont(self.font())
            font.setBold(style.bold)
            font.setItalic(style.italic)
            font.setUnderline(style.underline)
            font.setStrikeOut(style.strikethrough)
            self._font_cache[key] = font
        return self._font_cache[key]

    def _baseline_for_row(self, row: int) -> float:
        """Vertically center glyphs inside their fixed-height cells."""
        extra_height = self.cell_height - self._font_height
        return VERTICAL_PADDING + row * self.cell_height + extra_height / 2 + self._font_ascent

    def _refresh_font_metrics(self) -> None:
        """Cache font geometry once instead of rebuilding it for every glyph."""
        metrics = QFontMetricsF(self.font())
        self._cell_width = max(1, ceil(metrics.horizontalAdvance("M")))
        self._cell_height = max(1, ceil(metrics.lineSpacing()))
        self._font_height = metrics.height()
        self._font_ascent = metrics.ascent()
        self._font_cache.clear()
        self._resolved_style_cache.clear()

    def _refresh_palette_colors(self) -> None:
        """Cache the small set of theme colors used on every paint."""
        self._foreground_color = QColor(self._palette.foreground)
        self._background_color = QColor(self._palette.background)
        self._cursor_color = QColor(self._palette.cursor)
        self._cursor_text_color = QColor(self._palette.cursor_text)
        self._selection_background_color = QColor(self._palette.selection_background)
        self._selection_foreground_color = QColor(self._palette.selection_foreground)

    def _exposed_rows(self, region: QRegion) -> list[tuple[int, int, int]]:
        """Collapse a paint region into one visible column range per row."""
        row_ranges: dict[int, tuple[int, int]] = {}
        for rectangle in region:
            first_row = (rectangle.top() - VERTICAL_PADDING) // self.cell_height
            last_row = (rectangle.bottom() - VERTICAL_PADDING) // self.cell_height
            first_column = (rectangle.left() - HORIZONTAL_PADDING) // self.cell_width - 1
            last_column = (rectangle.right() - HORIZONTAL_PADDING) // self.cell_width

            if (
                last_row < 0
                or first_row >= self.screen.rows
                or last_column < 0
                or first_column >= self.screen.columns
            ):
                continue

            first_row = max(0, first_row)
            last_row = min(self.screen.rows - 1, last_row)
            first_column = max(0, first_column)
            last_column = min(self.screen.columns - 1, last_column)
            for row in range(first_row, last_row + 1):
                previous_range = row_ranges.get(row)
                if previous_range is None:
                    row_ranges[row] = (first_column, last_column)
                else:
                    row_ranges[row] = (
                        min(previous_range[0], first_column),
                        max(previous_range[1], last_column),
                    )

        return [
            (row, first_column, last_column)
            for row, (first_column, last_column) in sorted(row_ranges.items())
        ]

    def _update_selection_rows(
        self,
        old_selection: TerminalSelection | None,
        new_selection: TerminalSelection | None,
    ) -> None:
        """Repaint only rows touched by the old or new selection."""
        region = QRegion()
        document_top = self.verticalScrollBar().value()
        document_bottom = document_top + self.screen.rows - 1
        for selection in (old_selection, new_selection):
            if selection is None:
                continue
            first_row = max(selection.start.row, document_top)
            last_row = min(selection.end.row, document_bottom)
            if first_row > last_row:
                continue
            viewport_row = first_row - document_top
            top = VERTICAL_PADDING + viewport_row * self.cell_height
            height = (last_row - first_row + 1) * self.cell_height
            region += QRect(0, top, self.viewport().width(), height)
        if not region.isEmpty():
            self.viewport().update(region)


class _AccessibleTerminalView(QAccessibleWidget):
    """Expose a painted terminal as a stable, read-only accessible value."""

    def __init__(self, view: TerminalView) -> None:
        super().__init__(view, QAccessible.Role.Terminal)

    def text(self, text_type: QAccessible.Text) -> str:
        """Return viewport text on demand while keeping the object name stable."""
        view = self.widget()
        if not isinstance(view, TerminalView):
            return ""
        if text_type == QAccessible.Text.Value:
            return view.plain_text
        if text_type == QAccessible.Text.Description:
            return f"{view.accessibleDescription()} {view.accessible_status}".strip()
        return super().text(text_type)


def _terminal_accessible_factory(
    _class_name: str,
    obj: object,
) -> _AccessibleTerminalView | None:
    """Create the custom interface only for Termana's painted terminal view."""
    if isinstance(obj, TerminalView):
        return _AccessibleTerminalView(obj)
    return None


QAccessible.installFactory(_terminal_accessible_factory)
