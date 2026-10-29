"""Explicit, bounded grid selection; opening or cancelling changes no sessions."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.terminal_layout import (
    MAX_TERMINAL_PANES,
    TerminalPaneLayout,
    terminal_grid_layout,
)
from termana.widgets.form_controls import ThemedSpinBox
from termana.widgets.wrapping_label import WrappingLabel


class TerminalGridDialog(QDialog):
    """Select columns and rows and preview the total before applying."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("terminalGridDialog")
        self.setWindowTitle(tr("Custom terminal grid"))
        root = QVBoxLayout(self)
        root.setSpacing(12)
        hint = WrappingLabel(
            tr(
                "Choose columns and rows. Existing tabs and connections stay open; new panes start empty."
            )
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.columns = ThemedSpinBox(self)
        self.rows = ThemedSpinBox(self)
        for field, name, label in (
            (self.columns, "gridColumns", tr("Columns")),
            (self.rows, "gridRows", tr("Rows")),
        ):
            field.setObjectName(name)
            field.setAccessibleName(label)
            field.setRange(1, MAX_TERMINAL_PANES)
            field.setValue(2)
            form.addRow(label, field)
        root.addLayout(form)
        self.total = QLabel(self)
        self.total.setWordWrap(True)
        root.addWidget(self.total)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText(tr("Apply grid"))
        self.apply_button.setProperty("actionRole", "primary")
        self.apply_button.setAutoDefault(False)
        self.apply_button.setDefault(False)
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setProperty("actionRole", "secondary")
        cancel.setDefault(True)
        # QDialogButtonBox can polish its native Cocoa buttons on insertion,
        # before the dynamic actionRole is assigned. Refresh that cached style.
        for button in (self.apply_button, cancel):
            button.style().unpolish(button)
            button.style().polish(button)
            button.updateGeometry()
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self.columns.valueChanged.connect(self._update_total)
        self.rows.valueChanged.connect(self._update_total)
        self._update_total()

    def _update_total(self) -> None:
        count = self.columns.value() * self.rows.value()
        self.total.setText(
            tr("{p0} panes · maximum {p1}", p0=f"{count}", p1=f"{MAX_TERMINAL_PANES}")
        )
        self.apply_button.setEnabled(count <= MAX_TERMINAL_PANES)

    def accept(self) -> None:
        if self.columns.value() * self.rows.value() <= MAX_TERMINAL_PANES:
            super().accept()

    def selected_layout(self) -> TerminalPaneLayout:
        """Validate again at the model boundary, including programmatic callers."""
        return terminal_grid_layout(self.columns.value(), self.rows.value())
