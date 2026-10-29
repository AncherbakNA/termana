"""Bounded literal search across terminal scrollback and live rows."""

from dataclasses import dataclass
from typing import Final
from unicodedata import category

from termana.terminal.screen import TerminalScreenModel

MAX_SEARCH_QUERY_CHARACTERS: Final = 512
MAX_SEARCH_RESULTS: Final = 1000


@dataclass(frozen=True, order=True, slots=True)
class TerminalSearchMatch:
    """One match in absolute document coordinates with inclusive columns."""

    row: int
    start_column: int
    end_column: int


@dataclass(frozen=True, slots=True)
class TerminalSearchResult:
    """A bounded set of matches and any reason it is incomplete."""

    matches: tuple[TerminalSearchMatch, ...] = ()
    truncated: bool = False
    query_too_long: bool = False


def search_terminal_screen(
    screen: TerminalScreenModel,
    query: str,
    *,
    case_sensitive: bool = False,
    whole_words: bool = False,
    max_results: int = MAX_SEARCH_RESULTS,
) -> TerminalSearchResult:
    """Find non-overlapping literals in physical document rows.

    Rows are searched independently in deterministic document order: retained
    scrollback first, followed by the live screen. Match columns are zero-based
    and inclusive. A wide glyph's empty continuation cell is part of a match.

    The query is deliberately not stripped or normalized. An overlong query is
    rejected in the result instead of being silently changed. ``max_results``
    may lower the exported hard limit but cannot raise it. Searches use Unicode
    case folding unless ``case_sensitive`` is true. ``whole_words`` rejects
    matches adjacent to Unicode letters, digits, underscores or combining marks;
    punctuation and physical row edges delimit words. It does not enable regex.
    """
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise TypeError("max_results must be an integer")
    if not 1 <= max_results <= MAX_SEARCH_RESULTS:
        raise ValueError(f"max_results must be between 1 and {MAX_SEARCH_RESULTS}")
    if not isinstance(case_sensitive, bool):
        raise TypeError("case_sensitive must be a boolean")
    if not isinstance(whole_words, bool):
        raise TypeError("whole_words must be a boolean")
    if not query:
        return TerminalSearchResult()
    if len(query) > MAX_SEARCH_QUERY_CHARACTERS:
        return TerminalSearchResult(query_too_long=True)

    search_query = query if case_sensitive else query.casefold()
    if not search_query:
        return TerminalSearchResult()

    matches: list[TerminalSearchMatch] = []
    for row in range(screen.document_rows):
        row_text = screen.document_line_text(row)
        search_text = row_text if case_sensitive else row_text.casefold()
        match_start = search_text.find(search_query)
        if match_start < 0:
            continue

        # Mapping is unnecessary for the common rare/no-match row. Build it
        # only after the cheap text scan proves a highlight is needed.
        cell_texts = screen.document_row_cell_texts(row)
        search_columns = _row_columns(cell_texts, case_sensitive=case_sensitive)
        previous_end_column = -1

        while match_start >= 0:
            match_end = match_start + len(search_query)
            next_match_start = search_text.find(search_query, match_end)
            if whole_words and (
                (match_start > 0 and _is_word_character(search_text[match_start - 1]))
                or (match_end < len(search_text) and _is_word_character(search_text[match_end]))
            ):
                match_start = next_match_start
                continue
            start_column = search_columns[match_start]
            end_column = search_columns[match_end - 1]
            end_column = _include_wide_stub(cell_texts, end_column)

            # Case folding may expand one terminal cell (for example, ß -> ss).
            # Do not return two visual highlights over that same physical cell.
            if start_column <= previous_end_column:
                match_start = next_match_start
                continue

            match = TerminalSearchMatch(
                row=row,
                start_column=start_column,
                end_column=end_column,
            )
            if len(matches) == max_results:
                return TerminalSearchResult(matches=tuple(matches), truncated=True)
            matches.append(match)
            previous_end_column = end_column
            match_start = next_match_start

    return TerminalSearchResult(matches=tuple(matches))


def _is_word_character(character: str) -> bool:
    """Marks remain attached to their Unicode word rather than creating false edges."""
    return character == "_" or character.isalnum() or category(character).startswith("M")


def _row_columns(cell_texts: tuple[str, ...], *, case_sensitive: bool) -> tuple[int, ...]:
    """Map searched string indexes back to physical terminal columns."""
    search_columns: list[int] = []
    for column, text in enumerate(cell_texts):
        search_length = len(text if case_sensitive else text.casefold())
        if not search_length:
            continue
        search_columns.extend([column] * search_length)
    return tuple(search_columns)


def _include_wide_stub(cell_texts: tuple[str, ...], end_column: int) -> int:
    """Include the empty continuation cell following a matched wide glyph."""
    following_column = end_column + 1
    if following_column < len(cell_texts) and cell_texts[following_column] == "":
        return following_column
    return end_column
