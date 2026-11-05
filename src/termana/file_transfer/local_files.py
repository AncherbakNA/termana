"""Read-only filesystem presentation with app-owned size labels."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileSystemModel

from termana.ui.file_sizes import exact_file_size_text, file_size_text
from termana.ui.i18n import tr
from termana.ui.ui_text import plain_text_tooltip


class LocalFilesModel(QFileSystemModel):
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.ToolTipRole and index.column() != 1:
            detail = self.fileName(index)
            if not self.isDir(index):
                detail += "\n" + exact_file_size_text(self.size(index))
            return plain_text_tooltip(detail)
        if index.isValid() and index.column() == 1:
            if role == Qt.ItemDataRole.DisplayRole:
                return "—" if self.isDir(index) else file_size_text(self.size(index))
            if role == Qt.ItemDataRole.ToolTipRole and not self.isDir(index):
                return exact_file_size_text(self.size(index))
        return super().data(index, role)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if section == 1 and orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr("Size")
        return super().headerData(section, orientation, role)
