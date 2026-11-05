"""Plain-text result grid and presentation-only SQL highlighting."""

import re

from PySide6.QtCore import QAbstractTableModel, Qt
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat

from termana.database.results import QueryResult
from termana.ui.ui_text import plain_text_tooltip


class QueryTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.result = QueryResult()

    def replace_result(self, result):
        self.beginResetModel()
        self.result = result
        self.endResetModel()

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.result.rows)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.result.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < self.rowCount() and 0 <= index.column() < self.columnCount()):
            return None
        value = self.result.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return plain_text_tooltip("NULL" if value is None else value)
        if role == Qt.ItemDataRole.DisplayRole:
            return "NULL" if value is None else value
        if role == Qt.ItemDataRole.FontRole and value is None:
            from PySide6.QtGui import QFont
            font = QFont()
            font.setItalic(True)
            return font
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < self.columnCount():
            return self.result.columns[section]
        if orientation == Qt.Orientation.Vertical and 0 <= section < self.rowCount():
            return str(section + 1)
        return None


class SqlHighlighter(QSyntaxHighlighter):
    def set_colors(self, colors):
        self.keyword = QColor(colors.accent)
        self.comment = QColor(colors.muted_text)
        self.rehighlight()

    def highlightBlock(self, text):
        if not hasattr(self, "keyword"):
            return
        keyword = QTextCharFormat()
        keyword.setForeground(self.keyword)
        for match in re.finditer(r"\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|AS|AND|OR|NOT|NULL|IS|IN|ORDER|BY|GROUP|HAVING|LIMIT|OFFSET|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|VIEW|ALTER|DROP|BEGIN|COMMIT|ROLLBACK|WITH|DISTINCT|UNION)\b", text, re.I):
            self.setFormat(match.start(), match.end() - match.start(), keyword)
        comment = text.find("--")
        if comment >= 0:
            style = QTextCharFormat()
            style.setForeground(self.comment)
            self.setFormat(comment, len(text) - comment, style)
