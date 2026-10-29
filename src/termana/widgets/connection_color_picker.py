"""Compact connection accent picker, separate from terminal ANSI colors."""

from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QColorDialog, QHBoxLayout, QPushButton, QWidget

from termana.connection_colors import CONNECTION_COLORS
from termana.i18n import tr
from termana.models.connection import ConnectionProfile
from termana.widgets.form_controls import FormComboBox


class ConnectionColorPicker(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.input = FormComboBox()
        self.input.setObjectName("connectionColorInput")
        self.input.setAccessibleName(tr("Connection color"))
        self.input.addItem(tr("Default"), None)
        for label, color in CONNECTION_COLORS:
            self.input.addItem(self._swatch(color), tr(label), color)
        self._custom_index = self.input.count()
        self.custom_button = QPushButton(tr("Custom…"))
        self.custom_button.setObjectName("connectionCustomColorButton")
        self.custom_button.setProperty("actionRole", "secondary")
        self.custom_button.setAutoDefault(False)
        self.custom_button.setAccessibleName(tr("Choose a custom connection color"))
        self.custom_button.clicked.connect(self._choose_custom)
        layout.addWidget(self.input, 1)
        layout.addWidget(self.custom_button)
        self.setToolTip(tr("Colors the icon in the sessions list and tab. Terminal text is unchanged."))

    @staticmethod
    def _swatch(color):
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(color))
        return QIcon(pixmap)

    @property
    def selected_color(self) -> str | None:
        return self.input.currentData()

    def set_color(self, color: str | None) -> None:
        # Reuse the model's exact public validation/canonicalization.
        color = ConnectionProfile("Color", "color.invalid", accent_color=color).accent_color
        index = self.input.findData(color)
        if index < 0:
            index = self._custom_index
            if self.input.count() == index:
                self.input.addItem("")
            self.input.setItemIcon(index, self._swatch(color))
            self.input.setItemText(index, tr("Custom ({p0})", p0=color))
            self.input.setItemData(index, color)
        self.input.setCurrentIndex(index)

    def _choose_custom(self) -> None:
        color = QColorDialog.getColor(QColor(self.selected_color or "#4778DD"), self,
            tr("Connection color"), QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if color.isValid():
            self.set_color(color.name())
