"""Accept local URL data as COPY only; never move a source or interpret text."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QTableWidget

MAX_DROP_FILES = 64


def local_drop_paths(mime):
    if not mime.hasUrls():
        return None
    urls = mime.urls()
    if not 1 <= len(urls) <= MAX_DROP_FILES:
        return None
    result = []
    for url in urls:
        if not url.isLocalFile() or url.host() not in ("", "localhost"):
            return None
        path = Path(url.toLocalFile())
        if not path.is_absolute():
            return None
        result.append(path)
    return tuple(result)


class SftpDropTable(QTableWidget):
    files_dropped = Signal(object)

    def __init__(self, rows, columns):
        super().__init__(rows, columns)
        self.drop_available = lambda: False
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setAcceptDrops(True)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def _review_event(self, event):
        if (
            not event.possibleActions() & Qt.DropAction.CopyAction
            or not self.drop_available()
            or local_drop_paths(event.mimeData()) is None
        ):
            event.ignore()
            return False
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        return True

    def dragEnterEvent(self, event):
        self._review_event(event)

    def dragMoveEvent(self, event):
        self._review_event(event)

    def dropEvent(self, event):
        if self._review_event(event):
            paths = local_drop_paths(event.mimeData())
            self.files_dropped.emit(paths)
