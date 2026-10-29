"""Literal terminal highlighting settings, staged until Preferences is saved."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from termana.models.terminal_highlighting import HighlightRule, TerminalHighlighting
from termana.ui_typography import scale_widget_font
from termana.widgets.centered_check_delegate import CenteredCheckDelegate
from termana.widgets.themed_checkbox import ThemedCheckBox


class TerminalHighlightingDialog(QDialog):
    def __init__(self, settings: TerminalHighlighting, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Terminal highlighting")
        self.resize(760, 440)
        self.selected_settings = settings
        layout = QVBoxLayout(self)
        self.enabled = ThemedCheckBox("Highlight words and IP addresses")
        self.enabled.setChecked(settings.enabled)
        layout.addWidget(self.enabled)
        hint = QLabel(
            "Separate words with commas; case does not matter. Colors use #RRGGBB."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        # Header size queries can polish the table during construction. It must
        # already be a child, not a temporary window eligible for a caption.
        self.rules = QTableWidget(0, 5, self)
        # Native class-specific table fonts can differ from the application font
        # used by styled headers. Keep paint and size-hint metrics in agreement.
        for view in (self.rules, self.rules.horizontalHeader(), self.rules.verticalHeader()):
            scale_widget_font(view, 1.0)
        self.rules.setWordWrap(False)
        self.rules.setAccessibleName("Highlighting rules")
        self.rules.setHorizontalHeaderLabels(("On", "Group", "Words", "Color", "IP addresses"))
        self.rules.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.rules.setColumnWidth(0, 40)
        for column in (1, 3):
            self.rules.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self._switch_delegate = CenteredCheckDelegate(self.rules)
        for column in (0, 4):
            self.rules.setItemDelegateForColumn(column, self._switch_delegate)
            self.rules.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.rules.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.rules.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rules.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.rules)
        controls = QHBoxLayout()
        for label, callback in (
            ("Add group", self._add),
            ("Remove group", self._remove),
            ("Reset defaults", self._reset),
        ):
            button = QPushButton(label)
            button.setAutoDefault(False)
            button.setProperty("actionRole", "secondary")
            button.clicked.connect(callback)
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setObjectName("errorText")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        for label, role, style in (
            ("Save", QDialogButtonBox.ButtonRole.AcceptRole, "primary"),
            ("Cancel", QDialogButtonBox.ButtonRole.RejectRole, "secondary"),
        ):
            button = QPushButton(label)
            button.setProperty("actionRole", style)
            buttons.addButton(button, role)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._populate(settings)

    def _populate(self, settings):
        self.rules.setRowCount(0)
        for rule in settings.rules:
            self._append(rule)

    def _append(self, rule):
        row = self.rules.rowCount()
        self.rules.insertRow(row)
        for column, text in ((1, rule.name), (2, ", ".join(rule.words)), (3, rule.color)):
            self.rules.setItem(row, column, QTableWidgetItem(text))
        for column, checked in ((0, rule.enabled), (4, rule.addresses)):
            item = QTableWidgetItem()
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
            )
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.rules.setItem(row, column, item)

    def _add(self):
        if self.rules.rowCount() < 16:
            self._append(HighlightRule("Custom", (), "#ae75da"))

    def _remove(self):
        if self.rules.currentRow() >= 0:
            self.rules.removeRow(self.rules.currentRow())

    def _reset(self):
        self.enabled.setChecked(True)
        self._populate(TerminalHighlighting())

    def _save(self):
        try:
            rules = []
            for row in range(self.rules.rowCount()):
                rules.append(
                    HighlightRule(
                        self.rules.item(row, 1).text().strip(),
                        tuple(
                            word.strip()
                            for word in self.rules.item(row, 2).text().split(",")
                            if word.strip()
                        ),
                        self.rules.item(row, 3).text().strip(),
                        self.rules.item(row, 0).checkState() == Qt.CheckState.Checked,
                        self.rules.item(row, 4).checkState() == Qt.CheckState.Checked,
                    )
                )
            self.selected_settings = TerminalHighlighting(self.enabled.isChecked(), tuple(rules))
        except (ValueError, TypeError) as error:
            self.error.setText(str(error))
            return
        self.accept()
