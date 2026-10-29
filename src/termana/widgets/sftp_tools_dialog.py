"""Transient, owner-bound SFTP tools, leaving the file list in its own viewport."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from termana.dialog_geometry import fit_dialog_to_screen
from termana.i18n import tr


class SftpToolsDialog(QDialog):
    def __init__(self, browser, note):
        super().__init__(browser, Qt.WindowType.Popup)
        self.setWindowTitle(tr("More remote file actions"))
        self.setMinimumWidth(300)
        self.resize(440, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.pages = {}
        self.page_layouts = {}
        for key, title in (
            ("actions", tr("More file actions")),
            ("display", tr("Display options")),
            ("places", tr("Quick folders")),
        ):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(6, 6, 6, 6)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, title)
            self.pages[key] = scroll
            self.page_layouts[key] = page_layout
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def show_page(self, key):
        self.tabs.setCurrentWidget(self.pages[key])
        self.show()
        QTimer.singleShot(0, self, lambda: fit_dialog_to_screen(self))
