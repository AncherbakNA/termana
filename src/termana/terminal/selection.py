"""Selection values and text extraction for a terminal cell grid."""

from dataclasses import dataclass
from unicodedata import category

from termana.terminal.screen import TerminalScreenModel


@dataclass(frozen=True, order=True, slots=True)
class CellPosition:
    """A zero-based cell position in the terminal document, including history."""

    row: int
    column: int


@dataclass(frozen=True, slots=True)
class TerminalSelection:
    """An inclusive linear selection between two terminal document cells."""

    anchor: CellPosition
    head: CellPosition

    @property
    def start(self) -> CellPosition:
        """Return the earlier endpoint in display order."""
        return min(self.anchor, self.head)

    @property
    def end(self) -> CellPosition:
        """Return the later endpoint in display order."""
        return max(self.anchor, self.head)

    def contains(self, row: int, column: int) -> bool:
        """Return whether a cell is inside the range without allocating values."""
        start = self.start
        end = self.end
        if row < start.row or row > end.row:
            return False
        if row == start.row and column < start.column:
            return False
        return row != end.row or column <= end.column

    def extract_text(self, screen: TerminalScreenModel) -> str:
        """Copy selected cells, omitting wide-character stubs and row padding."""
        lines: list[str] = []
        for row in range(self.start.row, self.end.row + 1):
            first_column = self.start.column if row == self.start.row else 0
            last_column = self.end.column if row == self.end.row else screen.columns - 1
            cells = screen.document_row_cells(row)
            line = "".join(cell.text for cell in cells[first_column : last_column + 1])
            lines.append(line if row == self.end.row else line.rstrip(" "))
        return "\n".join(lines)


def select_token(screen: TerminalScreenModel, position: CellPosition) -> TerminalSelection:
    """Select one row-local word, whitespace run, or punctuation glyph.

    Common address and path characters belong to words. Wide continuation
    cells stay with their glyph, and combining text is classified as one cell.
    Pointer coordinates outside the document are clamped to its nearest cell.
    """
    row = min(max(position.row, 0), screen.document_rows - 1)
    texts = screen.document_row_cell_texts(row)
    start = min(max(position.column, 0), len(texts) - 1)
    while start > 0 and not texts[start]:
        start -= 1

    kind = _token_kind(texts[start])
    end = start
    while end + 1 < len(texts) and not texts[end + 1]:
        end += 1

    if kind != "punctuation":
        while start > 0:
            previous = start - 1
            while previous > 0 and not texts[previous]:
                previous -= 1
            if _token_kind(texts[previous]) != kind:
                break
            start = previous
        while end + 1 < len(texts):
            following = end + 1
            if texts[following] and _token_kind(texts[following]) != kind:
                break
            end = following

    return TerminalSelection(CellPosition(row, start), CellPosition(row, end))


def _token_kind(text: str) -> str:
    """Classify an entire terminal glyph without splitting combining marks."""
    if text.isspace():
        return "whitespace"
    if text and all(
        character in "_-./:@~%+\\" or category(character)[0] in "LNM" for character in text
    ):
        return "word"
    return "punctuation"
