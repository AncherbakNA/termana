"""Implemented terminal, desktop and file-client choices in Sessions."""

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QButtonGroup, QGridLayout, QSizePolicy, QWidget

from termana.models.connection import ConnectionProtocol
from termana.ui.i18n import tr
from termana.ui.ui_icons import interface_icon
from termana.ui.widgets.chrome_tool_button import ChromeToolButton, anchor_tooltip


class ConnectionProtocolSelector(QWidget):
    """Keep the profile editor's index/data interface without a hidden combo box."""

    currentIndexChanged = Signal(int)

    def __init__(self, parent=None, *, include_rdp=False):
        super().__init__(parent)
        self._values = (ConnectionProtocol.SSH.value, ConnectionProtocol.TELNET.value)
        self._labels = (tr("SSH"), tr("Telnet (unencrypted)"))
        captions = (tr("SSH"), tr("Telnet"))
        self._icons = ("server", "terminal")
        if include_rdp:
            self._values += ("rdp",)
            self._labels += (tr("RDP — remote desktop"),)
            captions += (tr("RDP"),)
            self._icons += ("desktop",)
        description = tr("Choose SSH, Telnet or an RDP remote desktop.") if include_rdp else tr(
            "Choose encrypted SSH or unencrypted Telnet."
        )
        self._values += ("sftp", "ftp", "tftp", "database")
        self._labels += (tr("SFTP — SSH file transfer"), tr("FTP — file transfer"), tr("TFTP — file transfer"), tr("Database — SQL client"))
        captions += (tr("SFTP"), tr("FTP"), tr("TFTP"), tr("Database"))
        self._icons += ("folder", "folder", "file", "database")
        description = tr("Choose a terminal, remote desktop, file-transfer or database connection.")
        self._index = 0
        self.buttons = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        for index, label in enumerate(captions):
            button = ChromeToolButton(self)
            anchor_tooltip(button)
            button.setObjectName(f"connectionType_{self._values[index]}")
            button.setText(label)
            button.setToolTip(self._labels[index])
            button.setAccessibleName(self._labels[index])
            button.setAccessibleDescription(description)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(24, 24))
            button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self._group.addButton(button, index)
            self.buttons.append(button)
            layout.addWidget(button, index // 3, index % 3)
        self.buttons[0].setChecked(True)
        self.setFocusProxy(self.buttons[0])
        self._group.idClicked.connect(self.setCurrentIndex)
        self._refresh_icons()

    def currentIndex(self) -> int:
        return self._index

    def currentData(self) -> str:
        return self._values[self._index]

    def findData(self, value: str) -> int:
        return self._values.index(value) if value in self._values else -1

    def itemText(self, index: int) -> str:
        return self._labels[index]

    def count(self) -> int:
        return len(self._values)

    def setCurrentIndex(self, index: int) -> None:
        if type(index) is not int or index not in range(len(self._values)):
            raise ValueError("Unsupported connection protocol index")
        self.buttons[index].setChecked(True)
        self.setFocusProxy(self.buttons[index])
        if index != self._index:
            self._index = index
            self.currentIndexChanged.emit(index)

    def _refresh_icons(self) -> None:
        color = self.palette().color(QPalette.ColorRole.WindowText).name()
        for button, name in zip(self.buttons, self._icons, strict=True):
            button.setIcon(interface_icon(name, color, size=24))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._refresh_icons()
