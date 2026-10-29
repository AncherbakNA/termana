"""A replaceable terminal-screen model built on top of pyte."""

import codecs
import unicodedata
from collections import deque
from collections.abc import Iterable, Iterator
from copy import copy
from dataclasses import dataclass, field, replace
from functools import lru_cache, partial
from typing import Any, Final

import pyte
from wcwidth import wcwidth

from termana.models.settings import DEFAULT_TERMINAL_SCROLLBACK_LINES
from termana.terminal.control_filter import OscTitle, TerminalControlFilter
from termana.terminal.mouse import MouseTrackingMode
from termana.terminal.title import sanitize_terminal_title

APPLICATION_CURSOR_KEYS_MODE = 1 << 5
BRACKETED_PASTE_MODE = 2004 << 5
NORMAL_MOUSE_TRACKING_MODE = 1000 << 5
BUTTON_EVENT_MOUSE_TRACKING_MODE = 1002 << 5
ANY_EVENT_MOUSE_TRACKING_MODE = 1003 << 5
SGR_MOUSE_REPORTING_MODE = 1006 << 5
MOUSE_PRIVATE_MODES: Final = frozenset({1000, 1002, 1003, 1006})
ALTERNATE_SCREEN_PRIVATE_MODES: Final = frozenset({47, 1047, 1049})
CURSOR_SAVE_PRIVATE_MODE: Final = 1048
CURSOR_STATE_PRIVATE_MODES: Final = frozenset({6, 7})
ORIGIN_MODE: Final = 6 << 5
AUTOWRAP_MODE: Final = 7 << 5
IGNORED_GEOMETRY_PRIVATE_MODES: Final = frozenset({3})
ALTERNATE_CLEAR_ON_ENTER_MODE: Final = 1049
ALTERNATE_CLEAR_ON_EXIT_MODES: Final = frozenset({1047})
DEFAULT_SCROLLBACK_LINES: Final = DEFAULT_TERMINAL_SCROLLBACK_LINES
DEFAULT_TAB_STOP_INTERVAL: Final = 8
MAX_COMBINING_CHARACTERS_PER_CELL: Final = 16
MAX_SHARED_SOURCE_CHARACTERS: Final = 4096


@dataclass(frozen=True, slots=True)
class CellStyle:
    """Visual attributes attached to one terminal cell."""

    foreground: str = "default"
    background: str = "default"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False


@dataclass(frozen=True, slots=True)
class TerminalCell:
    """A character and its immutable style, independent from pyte's API."""

    text: str
    style: CellStyle


@dataclass(frozen=True, slots=True)
class CursorPosition:
    """Zero-based terminal cursor coordinates."""

    row: int
    column: int
    hidden: bool


@dataclass(frozen=True, slots=True)
class ScreenUpdate:
    """Describe rows that changed and replies requested by an application."""

    dirty_rows: frozenset[int]
    replies: tuple[str, ...] = ()
    scrollback_lines_added: int = 0
    scrollback_lines_dropped: int = 0
    scrollback_cleared: bool = False
    window_title: str | None = None
    bell_requested: bool = False

    def __post_init__(self) -> None:
        """Keep event flags explicitly boolean at the parser boundary."""
        if type(self.bell_requested) is not bool:
            raise TypeError("bell_requested must be a bool")


@dataclass(frozen=True, slots=True)
class _SavedCursorState:
    """State stored by DECSC/1048 and restored without consuming the slot."""

    cursor: Any
    g0_charset: str
    g1_charset: str
    charset: int
    origin_mode: bool
    autowrap_mode: bool


class TerminalScreenError(RuntimeError):
    """Raised when malformed terminal data breaks the selected parser."""


@lru_cache(maxsize=2048)
def _cached_cell_style(
    foreground: str,
    background: str,
    bold: bool,
    italic: bool,
    underline: bool,
    strikethrough: bool,
    reverse: bool,
    blink: bool,
) -> CellStyle:
    """Share immutable styles instead of rebuilding them for every cell."""
    return CellStyle(
        foreground=foreground,
        background=background,
        bold=bold,
        italic=italic,
        underline=underline,
        strikethrough=strikethrough,
        reverse=reverse,
        blink=blink,
    )


@lru_cache(maxsize=4096)
def _cached_terminal_cell(text: str, style: CellStyle) -> TerminalCell:
    """Share common immutable snapshots, especially blank terminal cells."""
    return TerminalCell(text=text, style=style)


@lru_cache(maxsize=MAX_SHARED_SOURCE_CHARACTERS)
def _shared_source_character(source: Any) -> Any:
    """Intern immutable pyte Char values, without retaining entire history rows."""
    return source


def _terminal_cell_from_source(source: Any) -> TerminalCell:
    """Convert one internal pyte character into our immutable public value."""
    return _cached_terminal_cell(
        source.data,
        _cached_cell_style(
            source.fg,
            source.bg,
            source.bold,
            source.italics,
            source.underscore,
            source.strikethrough,
            source.reverse,
            source.blink,
        ),
    )


def _blank_terminal_cell() -> TerminalCell:
    """Return the shared default blank used to widen old history rows."""
    return _cached_terminal_cell(
        " ",
        _cached_cell_style(
            "default",
            "default",
            False,
            False,
            False,
            False,
            False,
            False,
        ),
    )


@dataclass(slots=True)
class _ScrollbackRow:
    """Keep a sparse immutable pyte snapshot and convert it on demand."""

    source_width: int
    source_characters: dict[int, Any]
    source_default_character: Any
    original_cells: tuple[TerminalCell, ...] | None = None
    fitted_columns: int | None = None
    fitted_cells: tuple[TerminalCell, ...] | None = None
    text_columns: int | None = None
    text_cache: str | None = None

    def cells_for_columns(self, columns: int) -> tuple[TerminalCell, ...]:
        """Pad or clip a physical row without attempting semantic reflow."""
        if self.original_cells is None:
            self.original_cells = tuple(
                _terminal_cell_from_source(
                    self.source_characters.get(column, self.source_default_character)
                )
                for column in range(self.source_width)
            )
        if self.source_width == columns:
            return self.original_cells
        if self.fitted_columns == columns and self.fitted_cells is not None:
            return self.fitted_cells

        if self.source_width > columns:
            cells = self.original_cells[:columns]
        else:
            cells = self.original_cells + (_blank_terminal_cell(),) * (columns - self.source_width)
        self.fitted_columns = columns
        self.fitted_cells = cells
        return cells

    def cell_texts_for_columns(self, columns: int) -> tuple[str, ...]:
        """Return physical cell text without materializing public styled cells."""
        retained_columns = min(self.source_width, columns)
        texts = tuple(
            self.source_characters.get(column, self.source_default_character).data
            for column in range(retained_columns)
        )
        if retained_columns < columns:
            texts += (" ",) * (columns - retained_columns)
        return texts

    def text_for_columns(self, columns: int) -> str:
        """Join physical text directly without allocating cells or a text tuple."""
        if self.text_columns == columns and self.text_cache is not None:
            return self.text_cache
        retained_columns = min(self.source_width, columns)
        texts = [self.source_default_character.data] * retained_columns
        for column, character in self.source_characters.items():
            if column < retained_columns:
                texts[column] = character.data
        text = "".join(texts)
        if retained_columns < columns:
            text += " " * (columns - retained_columns)
        self.text_columns = columns
        self.text_cache = text
        return text

    def is_default_blank(self) -> bool:
        """Return whether the sparse source contains only default blank cells."""
        blank = _blank_terminal_cell()
        return _terminal_cell_from_source(self.source_default_character) is blank and all(
            _terminal_cell_from_source(character) is blank
            for character in self.source_characters.values()
        )


@dataclass(frozen=True, slots=True)
class TerminalTextSnapshot:
    """A stable physical document whose text can be rendered off the GUI thread.

    Row sources are immutable after capture, and the private row wrappers own
    their caches independently of the live model. Retained text is deliberately
    excluded from the representation to avoid exposing terminal output in logs.
    """

    columns: int
    _rows: tuple[_ScrollbackRow, ...] = field(repr=False)

    def iter_lines(self) -> Iterator[str]:
        """Yield ANSI-free rows, preserving blank rows and non-space whitespace."""
        for row in self._rows:
            yield row.text_for_columns(self.columns).rstrip(" ")


class _TermanaPyteScreen(pyte.Screen):
    """Collect replies and physical rows that scroll beyond the top edge."""

    def __init__(
        self,
        columns: int,
        lines: int,
        scrollback_limit: int,
        *,
        capture_scrollback: bool,
        pending_replies: list[str],
    ) -> None:
        self.scrollback: deque[_ScrollbackRow] = deque(maxlen=scrollback_limit)
        self.capture_scrollback = capture_scrollback
        self.pending_replies = pending_replies
        self.pending_scrollback_lines_added = 0
        self.pending_scrollback_lines_dropped = 0
        self.pending_scrollback_cleared = False
        self.scroll_generation = 0
        super().__init__(columns, lines)

    def write_process_input(self, data: str) -> None:
        """Queue responses to terminal status and cursor-position queries."""
        self.pending_replies.append(data)

    def draw(self, data: str) -> None:
        """Bound combining work and skip unsupported zero-width data per character."""
        if data.isascii() and data.isprintable():
            # The common shell/log path has no zero-width or combining code
            # points. Pyte still owns character-set translation and wrapping.
            super().draw(data)
            return
        ordinary_characters: list[str] = []

        def flush_ordinary_characters() -> None:
            if ordinary_characters:
                super(_TermanaPyteScreen, self).draw("".join(ordinary_characters))
                ordinary_characters.clear()

        for character in data:
            character_width = wcwidth(character)
            if character_width > 0:
                ordinary_characters.append(character)
                continue
            if character_width < 0:
                continue

            flush_ordinary_characters()
            if not unicodedata.combining(character):
                continue

            previous_row = self.cursor.y if self.cursor.x else self.cursor.y - 1
            previous_column = self.cursor.x - 1 if self.cursor.x else self.columns - 1
            if previous_row < 0:
                continue
            previous = self.buffer[previous_row][previous_column]
            combining_count = sum(
                bool(unicodedata.combining(existing)) for existing in previous.data
            )
            if combining_count >= MAX_COMBINING_CHARACTERS_PER_CELL:
                continue
            super().draw(character)
            self.dirty.add(previous_row)

        flush_ordinary_characters()

    def reset(self) -> None:
        """Reset the active screen and discard saved lines like pyte history."""
        self._clear_scrollback()
        super().reset()

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        """Clip physical top rows independently of cursor modes and sparse storage."""
        old_columns = self.columns
        if lines is not None and lines < self.lines:
            clipped_rows = self.lines - lines
            self.capture_rows_before_resize(clipped_rows)
            retained_rows = {
                row - clipped_rows: cells
                for row, cells in self.buffer.items()
                if clipped_rows <= row < self.lines
            }
            self.buffer.clear()
            self.buffer.update(retained_rows)
            # pyte implements shrink through CSI delete-lines. That operation
            # depends on scrolling margins and leaves stale destination rows
            # when its sparse source row is absent. Geometry changes instead
            # move every physical row, including absent (blank) rows, exactly
            # once. Keep pyte responsible for width changes and growth only.
            self.lines = lines
            self.dirty.update(range(lines))
            self.set_margins()
        super().resize(lines=lines, columns=columns)

        if self.columns < old_columns:
            self.tabstops.intersection_update(range(self.columns))
            return
        if self.columns == old_columns:
            return

        first_new_stop = (
            (old_columns + DEFAULT_TAB_STOP_INTERVAL - 1) // DEFAULT_TAB_STOP_INTERVAL
        ) * DEFAULT_TAB_STOP_INTERVAL
        self.tabstops.update(range(first_new_stop, self.columns, DEFAULT_TAB_STOP_INTERVAL))

    def erase_in_display(self, how: int = 0, *args: object, **kwargs: object) -> None:
        """Honor CSI 3 J as an explicit request to erase saved lines."""
        super().erase_in_display(how, *args, **kwargs)
        if how == 3:
            self._clear_scrollback()

    def index(self) -> None:
        """Capture a physical row before a top-anchored region scrolls it out."""
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom and top == 0 and self.capture_scrollback:
            self._append_scrollback(self._capture_scrollback_row(top))
        super().index()

    def live_row_snapshot(self, row: int) -> tuple[TerminalCell, ...]:
        """Convert one pyte row into the parser-independent cell contract."""
        source_row = self.buffer[row]
        return tuple(
            _terminal_cell_from_source(source)
            for source in (source_row[column] for column in range(self.columns))
        )

    def live_row_cell_texts(self, row: int) -> tuple[str, ...]:
        """Return one live row's text cells without converting style metadata."""
        source_row = self.buffer[row]
        return tuple(source_row[column].data for column in range(self.columns))

    def live_row_text(self, row: int) -> str:
        """Join one live row without allocating public cells or a text tuple."""
        source_row = self.buffer[row]
        return "".join(source_row[column].data for column in range(self.columns))

    def capture_rows_before_resize(self, count: int) -> None:
        """Preserve precisely the physical top rows removed by height shrink."""
        if not self.capture_scrollback:
            return
        rows = [self._capture_scrollback_row(row) for row in range(min(count, self.lines))]
        if all(row.is_default_blank() for row in rows):
            return
        for row in rows:
            self._append_scrollback(row)

    def consume_scrollback_change(self) -> tuple[int, int, bool]:
        """Return and reset scrollback mutations produced by one model call."""
        change = (
            self.pending_scrollback_lines_added,
            self.pending_scrollback_lines_dropped,
            self.pending_scrollback_cleared,
        )
        self.pending_scrollback_lines_added = 0
        self.pending_scrollback_lines_dropped = 0
        self.pending_scrollback_cleared = False
        return change

    def _capture_scrollback_row(self, row: int) -> _ScrollbackRow:
        """Copy sparse pyte storage before the live buffer shifts in place."""
        source_row = self.buffer[row]
        return _ScrollbackRow(
            source_width=self.columns,
            # pyte Char values are immutable named tuples. Copying the sparse
            # mapping detaches the row before pyte shifts or reuses its buffer;
            # conversion to our public TerminalCell contract can wait until the
            # user actually reads this history row.
            source_characters={
                column: _shared_source_character(character)
                for column, character in source_row.items()
            },
            source_default_character=_shared_source_character(source_row.default),
        )

    def _append_scrollback(self, row: _ScrollbackRow) -> None:
        """Append one row while reporting bounded-deque eviction explicitly."""
        self.scroll_generation += 1
        if self.scrollback.maxlen == 0:
            return
        if len(self.scrollback) == self.scrollback.maxlen:
            self.pending_scrollback_lines_dropped += 1
        self.scrollback.append(row)
        self.pending_scrollback_lines_added += 1

    def _clear_scrollback(self) -> None:
        """Clear saved rows and record the change only when history existed."""
        removed_lines = len(self.scrollback)
        if not removed_lines:
            return
        self.scroll_generation += 1
        self.scrollback.clear()
        self.pending_scrollback_lines_dropped += removed_lines
        self.pending_scrollback_cleared = True


class _TerminalScreenBuffers:
    """Route pyte events to independent primary and alternate screens."""

    def __init__(
        self,
        columns: int,
        lines: int,
        scrollback_limit: int,
        pending_replies: list[str],
    ) -> None:
        self.primary = _TermanaPyteScreen(
            columns,
            lines,
            scrollback_limit,
            capture_scrollback=True,
            pending_replies=pending_replies,
        )
        self.alternate = _TermanaPyteScreen(
            columns,
            lines,
            0,
            capture_scrollback=False,
            pending_replies=pending_replies,
        )
        self._active = self.primary
        self._saved_cursor_states: dict[_TermanaPyteScreen, _SavedCursorState] = {}
        self.mouse_protocol_generation = 0
        self.terminal_reset_generation = 0
        self.window_title = ""
        self._pending_window_title: str | None = None
        self._pending_bell_requested = False
        self._event_forwarders = {
            event: partial(self._dispatch, event)
            for event in pyte.Stream.events
            if event
            not in {
                "reset",
                "reset_mode",
                "restore_cursor",
                "save_cursor",
                "bell",
                "set_icon_name",
                "set_mode",
                "set_title",
            }
        }

    @property
    def active(self) -> _TermanaPyteScreen:
        """Return the screen that currently receives parser events."""
        return self._active

    @property
    def alternate_active(self) -> bool:
        """Return whether the fixed-height alternate buffer is visible."""
        return self._active is self.alternate

    @property
    def screens(self) -> tuple[_TermanaPyteScreen, _TermanaPyteScreen]:
        """Return both buffers for coordinated resize and cache cleanup."""
        return self.primary, self.alternate

    def __getattr__(self, name: str) -> object:
        """Expose stable dynamic forwarders required by pyte.Stream."""
        try:
            return self._event_forwarders[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        """Handle DEC alternate/global modes and delegate all other modes."""
        if not kwargs.get("private"):
            for screen in self.screens:
                screen.set_mode(*modes, **kwargs)
            return
        if MOUSE_PRIVATE_MODES.intersection(modes):
            self.mouse_protocol_generation += 1

        save_cursor_on_entry = 1049 in modes
        clear_alternate = ALTERNATE_CLEAR_ON_ENTER_MODE in modes
        for mode in modes:
            if mode in IGNORED_GEOMETRY_PRIVATE_MODES:
                continue
            if mode == CURSOR_SAVE_PRIVATE_MODE:
                self.save_cursor()
            elif mode in ALTERNATE_SCREEN_PRIVATE_MODES:
                if save_cursor_on_entry:
                    self.save_cursor()
                    save_cursor_on_entry = False
                self._show_alternate(clear=clear_alternate)
                clear_alternate = False
            elif mode in CURSOR_STATE_PRIVATE_MODES:
                self.active.set_mode(mode, private=True)
            else:
                for screen in self.screens:
                    screen.set_mode(mode, private=True)

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        """Return from alternate or delegate ordinary mode resets."""
        if not kwargs.get("private"):
            for screen in self.screens:
                screen.reset_mode(*modes, **kwargs)
            return

        if MOUSE_PRIVATE_MODES.intersection(modes):
            self.mouse_protocol_generation += 1

        clear_alternate = self.alternate_active and bool(
            ALTERNATE_CLEAR_ON_EXIT_MODES.intersection(modes)
        )
        restore_cursor_on_exit = 1049 in modes
        for mode in modes:
            if mode in IGNORED_GEOMETRY_PRIVATE_MODES:
                continue
            if mode == CURSOR_SAVE_PRIVATE_MODE:
                self.restore_cursor()
            elif mode in ALTERNATE_SCREEN_PRIVATE_MODES:
                self._show_primary(
                    clear_alternate=clear_alternate,
                    restore_primary_cursor=(self.alternate_active and restore_cursor_on_exit),
                )
                if restore_cursor_on_exit:
                    self._restore_cursor_state(
                        self.primary,
                        preserve_screen_reverse=True,
                    )
                    restore_cursor_on_exit = False
                clear_alternate = False
            elif mode in CURSOR_STATE_PRIVATE_MODES:
                self.active.reset_mode(mode, private=True)
            else:
                for screen in self.screens:
                    screen.reset_mode(mode, private=True)
        if restore_cursor_on_exit:
            self._restore_cursor_state(
                self.primary,
                preserve_screen_reverse=True,
            )

    def save_cursor(self) -> None:
        """Save one persistent cursor slot for the currently active buffer."""
        self._save_cursor_state(self.active)

    def restore_cursor(self) -> None:
        """Restore the active buffer's cursor without consuming its slot."""
        self._restore_cursor_state(self.active)

    def bell(self, *args: object, **kwargs: object) -> None:
        """Coalesce audible/visual bell requests across both screen buffers."""
        self._pending_bell_requested = True

    def set_title(self, title: str) -> None:
        """Apply an OSC window title to the terminal rather than one buffer."""
        safe_title = sanitize_terminal_title(title)
        if safe_title == self.window_title:
            return
        self.window_title = safe_title
        self._pending_window_title = safe_title

    def set_icon_name(self, _icon_name: str) -> None:
        """Ignore OSC icon names until Termana has a consumer for that state."""

    def consume_window_title_change(self) -> str | None:
        """Return the latest completed OSC title once per successful feed."""
        title = self._pending_window_title
        self._pending_window_title = None
        return title

    def consume_bell_request(self) -> bool:
        """Return whether at least one BEL was parsed since the last update."""
        requested = self._pending_bell_requested
        self._pending_bell_requested = False
        return requested

    def reset(self) -> None:
        """Apply RIS to the whole terminal and return to a clean primary screen."""
        previous_window_title = self.window_title
        self.mouse_protocol_generation += 1
        self.terminal_reset_generation += 1
        self._saved_cursor_states.clear()
        for screen in self.screens:
            screen.reset()
            screen.savepoints.clear()
        self.window_title = ""
        if previous_window_title:
            self._pending_window_title = ""
        self._active = self.primary
        self._mark_active_dirty()

    def resize_saved_cursor_states(
        self,
        *,
        old_rows: int,
        rows: int,
        columns: int,
    ) -> None:
        """Keep persistent cursor slots aligned with clipped terminal rows."""
        clipped_rows = max(0, old_rows - rows)
        for screen, state in tuple(self._saved_cursor_states.items()):
            cursor = copy(state.cursor)
            cursor.y = min(max(0, cursor.y - clipped_rows), rows - 1)
            cursor.x = min(max(0, cursor.x), columns)
            self._saved_cursor_states[screen] = replace(state, cursor=cursor)

    def _dispatch(self, event: str, *args: object, **kwargs: object) -> object:
        """Resolve the active screen at call time, not at stream creation."""
        handler = getattr(self.active, event)
        return handler(*args, **kwargs)

    def _show_alternate(self, *, clear: bool) -> None:
        """Switch to the alternate buffer, optionally clearing its display."""
        if self.alternate_active:
            if clear:
                self._clear_alternate_display()
                self._mark_active_dirty()
            return

        # Modes 47 and 1047 switch buffers without saving the cursor.  Mode
        # 1049 keeps the primary cursor frozen so it can be restored on exit,
        # but the alternate buffer still starts at the current position and
        # with the current rendition attributes.
        self._copy_cursor_state(self.primary, self.alternate)
        if clear:
            self._clear_alternate_display()
        self._active = self.alternate
        self._mark_active_dirty()

    def _show_primary(
        self,
        *,
        clear_alternate: bool,
        restore_primary_cursor: bool,
    ) -> None:
        """Switch back to primary and optionally discard alternate contents."""
        if not self.alternate_active:
            return
        if clear_alternate:
            self._clear_alternate_display()
        if not restore_primary_cursor:
            self._copy_cursor_state(self.alternate, self.primary)
        self._active = self.primary
        self._mark_active_dirty()

    def _clear_alternate_display(self) -> None:
        """Erase alternate cells without performing a terminal-wide reset."""
        self.alternate.erase_in_display(2)
        if self.alternate.cursor.x >= self.alternate.columns:
            self.alternate.cursor.x = self.alternate.columns - 1
        self.alternate.dirty.update(range(self.alternate.lines))

    def _copy_cursor_state(
        self,
        source: _TermanaPyteScreen,
        target: _TermanaPyteScreen,
    ) -> None:
        """Transfer the state that follows a non-saving buffer switch."""
        target.cursor = copy(source.cursor)
        target.g0_charset = source.g0_charset
        target.g1_charset = source.g1_charset
        target.charset = source.charset
        for mode in (ORIGIN_MODE, AUTOWRAP_MODE):
            if mode in source.mode:
                target.mode.add(mode)
            else:
                target.mode.discard(mode)

    def _save_cursor_state(self, screen: _TermanaPyteScreen) -> None:
        """Replace the screen's DECSC slot with its current cursor state."""
        self._saved_cursor_states[screen] = _SavedCursorState(
            cursor=copy(screen.cursor),
            g0_charset=screen.g0_charset,
            g1_charset=screen.g1_charset,
            charset=screen.charset,
            origin_mode=ORIGIN_MODE in screen.mode,
            autowrap_mode=AUTOWRAP_MODE in screen.mode,
        )

    def _restore_cursor_state(
        self,
        screen: _TermanaPyteScreen,
        *,
        preserve_screen_reverse: bool = False,
    ) -> None:
        """Restore a DECSC slot while retaining terminal-wide presentation state."""
        state = self._saved_cursor_states.get(screen)
        if state is None:
            screen.reset_mode(6, private=True)
            return

        cursor_hidden = screen.cursor.hidden
        screen_reverse = screen.cursor.attrs.reverse
        screen.cursor = copy(state.cursor)
        screen.cursor.hidden = cursor_hidden
        if preserve_screen_reverse:
            screen.cursor.attrs = screen.cursor.attrs._replace(reverse=screen_reverse)
        screen.g0_charset = state.g0_charset
        screen.g1_charset = state.g1_charset
        screen.charset = state.charset
        for mode, enabled in (
            (ORIGIN_MODE, state.origin_mode),
            (AUTOWRAP_MODE, state.autowrap_mode),
        ):
            if enabled:
                screen.mode.add(mode)
            else:
                screen.mode.discard(mode)
        pending_wrap = screen.cursor.x == screen.columns
        screen.ensure_hbounds()
        screen.ensure_vbounds(use_margins=True)
        if pending_wrap:
            screen.cursor.x = screen.columns

    def _mark_active_dirty(self) -> None:
        """Force repaint after the visible document changes identity."""
        self.active.dirty.update(range(self.active.lines))


class TerminalScreenModel:
    """Decode backend bytes and maintain a fixed-size terminal cell grid."""

    def __init__(
        self,
        columns: int = 80,
        rows: int = 24,
        *,
        encoding: str = "utf-8",
        scrollback_limit: int = DEFAULT_SCROLLBACK_LINES,
    ) -> None:
        """Create an empty screen with a replaceable parser boundary."""
        self._validate_size(columns, rows)
        if isinstance(scrollback_limit, bool) or not isinstance(scrollback_limit, int):
            raise TypeError("Terminal scrollback limit must be an integer.")
        if scrollback_limit < 0:
            raise ValueError("Terminal scrollback limit cannot be negative.")
        self._encoding = encoding
        self._decoder = self._create_decoder()
        self._pending_replies: list[str] = []
        self._buffers = _TerminalScreenBuffers(
            columns,
            rows,
            scrollback_limit,
            self._pending_replies,
        )
        self._stream = pyte.Stream(self._buffers)
        self._control_filter = TerminalControlFilter()
        self._clear_dirty_rows()
        self._row_cache: list[tuple[TerminalCell, ...] | None] = [None] * rows

    @property
    def _screen(self) -> _TermanaPyteScreen:
        """Return the active parser screen for internal model operations."""
        return self._buffers.active

    @property
    def columns(self) -> int:
        """Return the current number of screen columns."""
        return self._screen.columns

    @property
    def rows(self) -> int:
        """Return the current number of screen rows."""
        return self._screen.lines

    @property
    def scrollback_size(self) -> int:
        """Return the number of physical rows retained above the live screen."""
        if self.alternate_screen:
            return 0
        return len(self._buffers.primary.scrollback)

    @property
    def scroll_generation(self) -> int:
        """Count physical primary-row loss even when history retention is zero."""
        return self._buffers.primary.scroll_generation

    @property
    def scrollback_limit(self) -> int:
        """Return the immutable history capacity selected for this screen."""
        max_length = self._buffers.primary.scrollback.maxlen
        return 0 if max_length is None else max_length

    @property
    def alternate_screen(self) -> bool:
        """Return whether a full-screen application is using its private buffer."""
        return self._buffers.alternate_active

    @property
    def document_rows(self) -> int:
        """Return scrollback and live rows as one paintable document."""
        return self.scrollback_size + self.rows

    @property
    def cursor(self) -> CursorPosition:
        """Return a stable snapshot of the current cursor."""
        return CursorPosition(
            row=self._screen.cursor.y,
            column=self._screen.cursor.x,
            hidden=self._screen.cursor.hidden,
        )

    @property
    def application_cursor_keys(self) -> bool:
        """Return whether the child requested DECCKM cursor sequences."""
        return APPLICATION_CURSOR_KEYS_MODE in self._screen.mode

    @property
    def bracketed_paste(self) -> bool:
        """Return whether the child requested protected paste delimiters."""
        return BRACKETED_PASTE_MODE in self._screen.mode

    @property
    def mouse_tracking_mode(self) -> MouseTrackingMode | None:
        """Return the most capable active xterm mouse-tracking protocol."""
        modes = self._screen.mode
        if ANY_EVENT_MOUSE_TRACKING_MODE in modes:
            return MouseTrackingMode.ANY_EVENT
        if BUTTON_EVENT_MOUSE_TRACKING_MODE in modes:
            return MouseTrackingMode.BUTTON_EVENT
        if NORMAL_MOUSE_TRACKING_MODE in modes:
            return MouseTrackingMode.NORMAL
        return None

    @property
    def sgr_mouse_reporting(self) -> bool:
        """Return whether the child requested decimal SGR mouse encoding."""
        return SGR_MOUSE_REPORTING_MODE in self._screen.mode

    @property
    def mouse_protocol_generation(self) -> int:
        """Return a counter changed by every mouse protocol DECSET/DECRST or RIS."""
        return self._buffers.mouse_protocol_generation

    @property
    def terminal_reset_generation(self) -> int:
        """Return a counter changed only when RIS resets terminal-owned state."""
        return self._buffers.terminal_reset_generation

    @property
    def window_title(self) -> str:
        """Return the latest completed OSC 0/2 window title."""
        return self._buffers.window_title

    @property
    def plain_text(self) -> str:
        """Return visible rows as ordinary text for tests and accessibility."""
        lines = [line.rstrip() for line in self._screen.display]
        return "\n".join(lines).rstrip("\n")

    def feed(self, data: bytes) -> ScreenUpdate:
        """Incrementally decode and parse one arbitrary backend byte chunk."""
        return self.feed_text(self._decoder.decode(data))

    def feed_text(self, text: str) -> ScreenUpdate:
        """Parse trusted application text and report the resulting damage."""
        old_cursor_row = self._screen.cursor.y
        try:
            stream_tokens: list[str] = []
            for token in self._control_filter.feed(text):
                if isinstance(token, OscTitle):
                    if stream_tokens:
                        self._stream.feed("".join(stream_tokens))
                        stream_tokens.clear()
                    self._buffers.set_title(token.value)
                else:
                    stream_tokens.append(token)
            if stream_tokens:
                self._stream.feed("".join(stream_tokens))
        except Exception as error:
            # A failed generator cannot safely parse the next chunk. Keep the
            # current screen, replace only the stream, and let the UI report it.
            self._invalidate_rows(self._screen.dirty)
            self._buffers.consume_bell_request()
            self._stream = pyte.Stream(self._buffers)
            raise TerminalScreenError(str(error)) from error

        dirty_rows = {row for row in self._screen.dirty if 0 <= row < self.rows}
        dirty_rows.update((old_cursor_row, self._screen.cursor.y))
        self._invalidate_rows(dirty_rows)
        self._clear_dirty_rows()
        replies = tuple(self._pending_replies)
        self._pending_replies.clear()
        scrollback_added, scrollback_dropped, scrollback_cleared = (
            self._buffers.primary.consume_scrollback_change()
        )
        window_title = self._buffers.consume_window_title_change()
        bell_requested = self._buffers.consume_bell_request()
        return ScreenUpdate(
            dirty_rows=frozenset(dirty_rows),
            replies=replies,
            scrollback_lines_added=scrollback_added,
            scrollback_lines_dropped=scrollback_dropped,
            scrollback_cleared=scrollback_cleared,
            window_title=window_title,
            bell_requested=bell_requested,
        )

    def finish(self) -> ScreenUpdate:
        """Flush the incremental decoder after the backend has stopped."""
        try:
            return self.feed_text(self._decoder.decode(b"", final=True))
        finally:
            self._control_filter.finish()

    def resize(self, columns: int, rows: int) -> ScreenUpdate:
        """Resize the grid without leaking pyte's rows-first argument order."""
        self._validate_size(columns, rows)
        if (columns, rows) == (self.columns, self.rows):
            return ScreenUpdate(frozenset())

        old_rows = self.rows
        old_cursor_row = self._screen.cursor.y
        old_cursor_positions = {
            screen: (screen.cursor.y, screen.cursor.x) for screen in self._buffers.screens
        }
        clipped_rows = max(0, old_rows - rows)
        for screen in self._buffers.screens:
            screen.resize(lines=rows, columns=columns)
            old_row, old_column = old_cursor_positions[screen]
            screen.cursor.y = min(max(old_row - clipped_rows, 0), rows - 1)
            screen.cursor.x = min(max(old_column, 0), columns)
        self._buffers.resize_saved_cursor_states(
            old_rows=old_rows,
            rows=rows,
            columns=columns,
        )
        self._row_cache = [None] * rows
        dirty_rows = {row for row in self._screen.dirty if 0 <= row < rows}
        dirty_rows.add(self._screen.cursor.y)
        if 0 <= old_cursor_row < rows:
            dirty_rows.add(old_cursor_row)
        self._clear_dirty_rows()
        scrollback_added, scrollback_dropped, scrollback_cleared = (
            self._buffers.primary.consume_scrollback_change()
        )
        return ScreenUpdate(
            dirty_rows=frozenset(dirty_rows),
            scrollback_lines_added=scrollback_added,
            scrollback_lines_dropped=scrollback_dropped,
            scrollback_cleared=scrollback_cleared,
        )

    def cell(self, row: int, column: int) -> TerminalCell:
        """Return one immutable cell snapshot using zero-based coordinates."""
        if not 0 <= column < self.columns:
            raise IndexError("Terminal cell coordinates are outside the screen.")
        return self.row_cells(row)[column]

    def row_cells(self, row: int) -> tuple[TerminalCell, ...]:
        """Return a cached immutable snapshot of one terminal row."""
        if not 0 <= row < self.rows:
            raise IndexError("Terminal row is outside the screen.")

        cached_row = self._row_cache[row]
        if cached_row is not None:
            return cached_row

        cached_row = self._screen.live_row_snapshot(row)
        self._row_cache[row] = cached_row
        return cached_row

    def row_cell_texts(self, row: int) -> tuple[str, ...]:
        """Return physical live-row text without materializing styled cells."""
        if not 0 <= row < self.rows:
            raise IndexError("Terminal row is outside the screen.")
        return self._screen.live_row_cell_texts(row)

    def document_row_cells(self, row: int) -> tuple[TerminalCell, ...]:
        """Return one absolute row from ``[history, live screen]``."""
        if not 0 <= row < self.document_rows:
            raise IndexError("Terminal document row is outside the screen and history.")
        if row < self.scrollback_size:
            return self._buffers.primary.scrollback[row].cells_for_columns(self.columns)
        return self.row_cells(row - self.scrollback_size)

    def document_row_cell_texts(self, row: int) -> tuple[str, ...]:
        """Return physical history/live text without materializing styled cells."""
        if not 0 <= row < self.document_rows:
            raise IndexError("Terminal document row is outside the screen and history.")
        if row < self.scrollback_size:
            return self._buffers.primary.scrollback[row].cell_texts_for_columns(self.columns)
        return self.row_cell_texts(row - self.scrollback_size)

    def document_cell(self, row: int, column: int) -> TerminalCell:
        """Return one cell using absolute scrollback-document coordinates."""
        if not 0 <= column < self.columns:
            raise IndexError("Terminal cell coordinates are outside the screen.")
        return self.document_row_cells(row)[column]

    def document_line_text(self, row: int) -> str:
        """Return one absolute history/live row as physical terminal text."""
        if not 0 <= row < self.document_rows:
            raise IndexError("Terminal document row is outside the screen and history.")
        if row < self.scrollback_size:
            return self._buffers.primary.scrollback[row].text_for_columns(self.columns)
        return self._screen.live_row_text(row - self.scrollback_size)

    def capture_text_snapshot(self) -> TerminalTextSnapshot:
        """Detach the active history and live rows without formatting their text.

        Capture on the model's owning thread; the returned snapshot may then be
        iterated by a worker while the terminal receives output or resizes.
        Alternate-screen history is empty, so hidden primary output is omitted.
        """
        screen = self._screen
        # History source mappings and pyte Char values are immutable by contract.
        # Copy only wrappers so worker-side formatting cannot mutate GUI caches.
        rows = [copy(row) for row in screen.scrollback]
        rows.extend(screen._capture_scrollback_row(row) for row in range(screen.lines))
        return TerminalTextSnapshot(columns=screen.columns, _rows=tuple(rows))

    def line_text(self, row: int) -> str:
        """Return one display row, padded to its terminal-cell width."""
        if not 0 <= row < self.rows:
            raise IndexError("Terminal row is outside the screen.")
        return self._screen.display[row]

    def _create_decoder(self) -> codecs.IncrementalDecoder:
        """Build a decoder that preserves characters split across chunks."""
        return codecs.getincrementaldecoder(self._encoding)(errors="replace")

    def _invalidate_rows(self, rows: Iterable[int]) -> None:
        """Drop snapshots only for rows the parser reports as changed."""
        for row in rows:
            if 0 <= row < len(self._row_cache):
                self._row_cache[row] = None

    def _clear_dirty_rows(self) -> None:
        """Clear pyte damage from both buffers after one public operation."""
        for screen in self._buffers.screens:
            screen.dirty.clear()

    @staticmethod
    def _validate_size(columns: int, rows: int) -> None:
        """Reject dimensions that cannot represent a usable terminal."""
        if columns < 1 or rows < 1:
            raise ValueError("Terminal dimensions must be positive.")
