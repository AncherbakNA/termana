"""Bounded, memory-only database results; never a query or credential log."""

from dataclasses import dataclass, field

MAX_QUERY_BYTES = 128 * 1024
MAX_RESULT_ROWS = 1000
MAX_RESULT_COLUMNS = 256
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_CELL_CHARACTERS = 64 * 1024
MAX_WIRE_BYTES = 16 * 1024 * 1024


class ResultLimitError(ValueError):
    """The reply cannot be safely buffered; retire the owning connection."""


class DatabaseComponentError(RuntimeError):
    """Installed driver differs from the reviewed private adapter contract."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str | None, ...], ...] = field(default=(), repr=False)
    affected: int | None = None
    truncated: bool = False
    transaction_open: bool = False


def validate_query(query):
    if type(query) is not str or not query.strip() or "\0" in query:
        raise ValueError("Enter a SQL statement")
    try:
        if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise ValueError("The SQL statement exceeds 128 KiB")
    except UnicodeError:
        raise ValueError("SQL must be valid Unicode") from None
    return query


def _cell(value):
    if value is None:
        return None, False
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = "0x" + bytes(value[: MAX_CELL_CHARACTERS // 2]).hex()
        shortened = len(value) > MAX_CELL_CHARACTERS
    else:
        value = str(value)
        shortened = len(value) > MAX_CELL_CHARACTERS
    if shortened:
        value = value[: MAX_CELL_CHARACTERS - 1] + "…"
    return value, shortened


def collect_result(cursor, *, transaction_open=False):
    """Fetch a bounded preview without rewriting or re-executing the user's SQL."""
    if cursor.description is None:
        count = cursor.rowcount
        return QueryResult(affected=count if type(count) is int and count >= 0 else None,
                           transaction_open=transaction_open)
    if len(cursor.description) > MAX_RESULT_COLUMNS:
        raise ResultLimitError("The result exceeds 256 columns")
    columns = tuple(str(column[0])[:1024] for column in cursor.description)
    rows, size, truncated = [], sum(len(c.encode("utf-8")) for c in columns), False
    while len(rows) <= MAX_RESULT_ROWS:
        values = cursor.fetchone()
        if values is None:
            break
        if len(rows) == MAX_RESULT_ROWS:
            truncated = True
            break
        row = []
        for value in values:
            text, shortened = _cell(value)
            truncated |= shortened
            size += len(text.encode("utf-8", errors="replace")) if text is not None else 0
            if size > MAX_RESULT_BYTES:
                return QueryResult(columns, tuple(rows), truncated=True,
                                   transaction_open=transaction_open)
            row.append(text)
        rows.append(tuple(row))
    return QueryResult(columns, tuple(rows), truncated=truncated,
                       transaction_open=transaction_open)
