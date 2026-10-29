"""Command preview, explicit insertion preserving line breaks and library management."""

from uuid import UUID

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.command_snippet import CommandSnippet
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.numbered_plain_text_edit import NumberedPlainTextEdit
from termana.widgets.wrapping_label import WrappingLabel


class CommandSnippetsSidebar(QWidget):
    """Selecting a command never sends input to any terminal."""

    insert_requested = Signal(object)
    manage_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._snippets: tuple[CommandSnippet, ...] = ()
        self._insertion_available = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search commands…"))
        self.search.setAccessibleName(tr("Search saved commands"))
        self.notice = WrappingLabel(tr("No saved commands yet."))
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.items = QListWidget()
        self.items.setAccessibleName(tr("Saved commands"))
        self.items.setMinimumWidth(0)
        self.preview = NumberedPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName(tr("Command preview"))
        self.preview.setMinimumWidth(0)
        self.insert_button = QPushButton(tr("Insert"))
        self.insert_button.setToolTip(
            tr("Every line break sends Enter, including a final line break.")
        )
        self.manage_button = QPushButton(tr("Manage…"))
        for button in (self.insert_button, self.manage_button):
            button.setProperty("actionRole", "secondary")
        for widget in (self.search, self.notice, self.items, self.preview):
            layout.addWidget(widget, 1 if widget is self.items else 0)
        layout.addWidget(AdaptiveButtonRow((self.insert_button, self.manage_button)))
        self.search.textChanged.connect(self._refilter)
        self.items.currentItemChanged.connect(self._select)
        self.items.itemActivated.connect(lambda _item: self._manage())
        self.insert_button.clicked.connect(self._insert)
        self.manage_button.clicked.connect(self._manage)
        self._select()

    def set_snippets(self, snippets, error=None):
        selected = self._selected_id()
        self._snippets = tuple(snippets)
        self.items.clear()
        for snippet in self._snippets:
            item = QListWidgetItem(snippet.name)
            item.setData(Qt.ItemDataRole.UserRole, snippet.snippet_id)
            self.items.addItem(item)
            if snippet.snippet_id == selected:
                self.items.setCurrentItem(item)
        self.notice.setText(error or tr("No saved commands yet."))
        self.notice.setVisible(bool(error) or not self._snippets)
        self._refilter()

    def _selected_id(self) -> UUID | None:
        item = self.items.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def set_insertion_available(self, available: bool):
        self._insertion_available = available
        self._select()

    def _refilter(self, _text=None):
        query = self.search.text().strip().casefold()
        for index in range(self.items.count()):
            item = self.items.item(index)
            item.setHidden(query not in item.text().casefold())
        self._select()

    def _select(self, *_args):
        item = self.items.currentItem()
        selected = self._selected_id() if item is not None and not item.isHidden() else None
        snippet = next((s for s in self._snippets if s.snippet_id == selected), None)
        command = snippet.command if snippet else ""
        if self.preview.toPlainText() != command:
            self.preview.setPlainText(command)
        self.insert_button.setEnabled(snippet is not None and self._insertion_available)

    def _insert(self, _checked=False):
        if self.insert_button.isEnabled():
            self.insert_requested.emit(self._selected_id())

    def _manage(self, _checked=False):
        item = self.items.currentItem()
        selected = self._selected_id() if item is not None and not item.isHidden() else None
        self.manage_requested.emit(selected)
