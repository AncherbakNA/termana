"""Literal matching rows in frozen output, never live indexing or row navigation."""

from collections.abc import Callable
from unicodedata import category

from termana.models.terminal_multi_search import (
    MAX_MULTI_SEARCH_CELLS,
    MAX_MULTI_SEARCH_PREVIEW,
    MAX_MULTI_SEARCH_QUERY,
    MAX_MULTI_SEARCH_RESULTS,
    MAX_MULTI_SEARCH_ROWS,
    MAX_MULTI_SEARCH_SESSIONS,
    TerminalMultiSearchResult,
    TerminalSearchDocument,
    TerminalSearchHit,
)


def _word(character: str) -> bool:
    return character == "_" or character.isalnum() or category(character).startswith("M")


def _preview(text: str, match_start: int, *, case_sensitive: bool) -> str:
    original_start = match_start
    if not case_sensitive:
        folded = 0
        for index, character in enumerate(text):
            if folded + len(character.casefold()) > match_start:
                original_start = index
                break
            folded += len(character.casefold())
    start = max(0, original_start - 60)
    excerpt = text[start : start + MAX_MULTI_SEARCH_PREVIEW - 2]
    safe = "".join(
        "\N{REPLACEMENT CHARACTER}"
        if category(char).startswith("C") or char in "\u2028\u2029"
        else char
        for char in excerpt
    )
    return ("…" if start else "") + safe + ("…" if start + len(excerpt) < len(text) else "")


def search_terminal_snapshots(
    documents: tuple[TerminalSearchDocument, ...],
    query: str,
    *,
    case_sensitive: bool = False,
    whole_words: bool = False,
    cancelled: Callable[[], bool] = lambda: False,
) -> TerminalMultiSearchResult:
    """Return one hit per matching physical row, with explicit global budgets.

    Source snapshots omit trailing padding spaces, like output export. Results
    identify captured rows only, never coordinates safe for a later live jump.
    """
    if type(documents) is not tuple or len(documents) > MAX_MULTI_SEARCH_SESSIONS:
        raise ValueError("Invalid bounded terminal search scope")
    if any(type(item) is not TerminalSearchDocument for item in documents):
        raise TypeError("Invalid terminal search source")
    if type(query) is not str or type(case_sensitive) is not bool or type(whole_words) is not bool:
        raise TypeError("Invalid literal search options")
    if len(query) > MAX_MULTI_SEARCH_QUERY:
        return TerminalMultiSearchResult(query_too_long=True)
    if not query:
        return TerminalMultiSearchResult()
    needle = query if case_sensitive else query.casefold()
    if not needle:
        return TerminalMultiSearchResult()
    hits = []
    rows, cells = 0, 0
    for document in documents:
        if cancelled():
            return TerminalMultiSearchResult(cancelled=True, rows_scanned=rows)
        if document.snapshot.columns > MAX_MULTI_SEARCH_CELLS:
            return TerminalMultiSearchResult(tuple(hits), truncated=True, rows_scanned=rows)
        for row_number, text in enumerate(document.snapshot.iter_lines(), 1):
            if cancelled():
                return TerminalMultiSearchResult(cancelled=True, rows_scanned=rows)
            rows += 1
            cells += document.snapshot.columns
            if rows > MAX_MULTI_SEARCH_ROWS or cells > MAX_MULTI_SEARCH_CELLS:
                return TerminalMultiSearchResult(tuple(hits), truncated=True, rows_scanned=rows - 1)
            searched = text if case_sensitive else text.casefold()
            position = searched.find(needle)
            while (
                position >= 0
                and whole_words
                and (
                    (position > 0 and _word(searched[position - 1]))
                    or (
                        position + len(needle) < len(searched)
                        and _word(searched[position + len(needle)])
                    )
                )
            ):
                position = searched.find(needle, position + len(needle))
            if position < 0:
                continue
            if len(hits) == MAX_MULTI_SEARCH_RESULTS:
                return TerminalMultiSearchResult(tuple(hits), truncated=True, rows_scanned=rows)
            session = document.session
            hits.append(
                TerminalSearchHit(
                    session.session_id,
                    session.title,
                    session.pane_number,
                    row_number,
                    _preview(text, position, case_sensitive=case_sensitive),
                )
            )
    return TerminalMultiSearchResult(tuple(hits), rows_scanned=rows)
