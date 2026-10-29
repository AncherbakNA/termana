"""Ephemeral bounded search snapshots; terminal output never appears in repr."""

from dataclasses import dataclass, field
from typing import Final
from unicodedata import category
from uuid import UUID

from termana.models.open_terminal import OpenTerminalSession
from termana.models.terminal_layout import MAX_TERMINAL_PANES
from termana.terminal.screen import TerminalTextSnapshot

MAX_MULTI_SEARCH_SESSIONS: Final = 16
MAX_MULTI_SEARCH_ROWS: Final = 100_000
MAX_MULTI_SEARCH_CELLS: Final = 4 * 1024 * 1024
MAX_MULTI_SEARCH_RESULTS: Final = 500
MAX_MULTI_SEARCH_QUERY: Final = 512
MAX_MULTI_SEARCH_PREVIEW: Final = 240


@dataclass(frozen=True, slots=True)
class TerminalSearchDocument:
    session: OpenTerminalSession = field(repr=False)
    snapshot: TerminalTextSnapshot = field(repr=False)

    def __post_init__(self):
        if (
            type(self.session) is not OpenTerminalSession
            or type(self.snapshot) is not TerminalTextSnapshot
        ):
            raise TypeError("Invalid frozen terminal search document")


@dataclass(frozen=True, slots=True)
class TerminalSearchSource:
    document: TerminalSearchDocument
    generation: object = field(repr=False, compare=False)

    def __post_init__(self):
        if type(self.document) is not TerminalSearchDocument or self.generation is None:
            raise TypeError("Invalid terminal search owner")


@dataclass(frozen=True, slots=True)
class TerminalSearchCapture:
    sources: tuple[TerminalSearchSource, ...] = ()
    skipped: int = 0
    budget_skipped: int = 0


@dataclass(frozen=True, slots=True)
class TerminalSearchHit:
    session_id: UUID
    title: str = field(repr=False)
    pane_number: int
    row_number: int
    preview: str = field(repr=False)

    def __post_init__(self):
        if type(self.session_id) is not UUID or type(self.title) is not str:
            raise TypeError("Invalid terminal search hit identity")
        if type(self.pane_number) is not int or not 1 <= self.pane_number <= MAX_TERMINAL_PANES:
            raise ValueError("Invalid terminal search pane")
        if type(self.row_number) is not int or not 1 <= self.row_number <= MAX_MULTI_SEARCH_ROWS:
            raise ValueError("Invalid terminal search snapshot row")
        if (
            type(self.preview) is not str
            or len(self.preview) > MAX_MULTI_SEARCH_PREVIEW
            or any(
                category(char).startswith("C") or char in "\u2028\u2029" for char in self.preview
            )
        ):
            raise ValueError("Invalid bounded terminal search preview")


@dataclass(frozen=True, slots=True)
class TerminalMultiSearchResult:
    hits: tuple[TerminalSearchHit, ...] = ()
    truncated: bool = False
    query_too_long: bool = False
    cancelled: bool = False
    rows_scanned: int = 0
