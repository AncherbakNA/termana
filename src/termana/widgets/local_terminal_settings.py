"""Local shell/start-folder preferences; never edit the user's shell startup files."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from termana.i18n import tr
from termana.models.settings import LocalTerminalSettings
from termana.terminal.local_preferences import available_local_shells, local_shell_command
from termana.widgets.form_controls import FormComboBox
from termana.widgets.wrapping_label import WrappingLabel


class LocalTerminalPreferences(QWidget):
    """Keep unavailable saved paths visible; check changed choices before saving."""

    def __init__(self, settings: LocalTerminalSettings, parent=None):
        super().__init__(parent)
        self._initial = settings
        self.invalid_control = None
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        self.directory = QLineEdit(settings.directory or "")
        self.directory.setObjectName("localStartDirectoryInput")
        self.directory.setAccessibleName(tr("Local start directory"))
        self.directory.setPlaceholderText(tr("Home directory"))
        self.directory.setMinimumWidth(0)
        self.directory.setMaxLength(8194)
        self.directory.setClearButtonEnabled(True)
        self.directory_browse = self._browse_button("browseLocalDirectoryButton")
        self.directory_browse.clicked.connect(self._browse_directory)
        directory_row = QHBoxLayout()
        directory_row.setSpacing(8)
        directory_row.addWidget(self.directory, 1)
        directory_row.addWidget(self.directory_browse)
        form.addRow(tr("Start directory"), directory_row)
        directory_hint = WrappingLabel(tr("New local terminals start here. Empty means your home folder."))
        directory_hint.setObjectName("mutedText")
        form.addRow(directory_hint)
        # setEditable() may polish the control before it reaches the row layout.
        self.shell = FormComboBox(self)
        self.shell.setObjectName("localShellInput")
        self.shell.setAccessibleName(tr("Local shell executable"))
        self.shell.setEditable(True)
        self.shell.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.shell.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        # Editable paths can scroll internally. Do not force a wide field plus
        # Browse beyond a narrow/large-font settings viewport.
        self.shell.setMinimumContentsLength(4)
        self.shell.setMinimumWidth(0)
        self.shell.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.shell.lineEdit().setMaxLength(8194)
        self.shell.addItem(tr("Automatic"), None)
        self.shell.setToolTip(tr("Automatic uses SHELL on Unix or COMSPEC on Windows."))
        self.shell.setAccessibleDescription(self.shell.toolTip())
        for label, path in available_local_shells():
            self.shell.addItem(f"{label} · {path}", path)
        if settings.shell is not None:
            index = self.shell.findData(settings.shell)
            if index < 0:
                self.shell.addItem(settings.shell, settings.shell)
                index = self.shell.count() - 1
            self.shell.setCurrentIndex(index)
        self.shell.lineEdit().setCursorPosition(0)
        self.shell.activated.connect(lambda _index: self.shell.lineEdit().setCursorPosition(0))
        self.shell_browse = self._browse_button("browseLocalShellButton")
        self.shell_browse.clicked.connect(self._browse_shell)
        shell_row = QHBoxLayout()
        shell_row.setSpacing(8)
        shell_row.addWidget(self.shell, 1)
        self.shell_custom = self.shell.custom_value_button(self)
        shell_row.addWidget(self.shell_browse)
        form.addRow(tr("Local shell"), shell_row)
        hint = WrappingLabel(
            tr(
                "Shell program for new local terminals. Custom… accepts an executable path."
            )
        )
        hint.setObjectName("mutedText")
        help_row = QHBoxLayout()
        help_row.addWidget(hint, 1)
        help_row.addWidget(self.shell_custom)
        form.addRow(help_row)

    @staticmethod
    def _browse_button(name):
        button = QPushButton(tr("Browse…"))
        button.setObjectName(name)
        button.setProperty("actionRole", "secondary")
        button.setAutoDefault(False)
        return button

    @property
    def selected_settings(self):
        self.invalid_control = self.directory
        directory = self.directory.text() or None
        # Validate each field separately so a hidden invalid row gets focus.
        LocalTerminalSettings(directory=directory)
        self.invalid_control = self.shell
        index = self.shell.currentIndex()
        shell = (
            self.shell.itemData(index)
            if index >= 0 and self.shell.currentText() == self.shell.itemText(index)
            else self.shell.currentText() or None
        )
        selected = LocalTerminalSettings(directory, shell)
        if selected != self._initial:
            self.invalid_control = (
                self.directory if directory != self._initial.directory else self.shell
            )
            local_shell_command(selected)
        self.invalid_control = None
        return selected

    def _browse_directory(self):
        selected = QFileDialog.getExistingDirectory(
            self, tr("Choose local start directory"), self.directory.text() or str(Path.home())
        )
        if selected:
            self.directory.setText(selected)

    def _browse_shell(self):
        selected, _ = QFileDialog.getOpenFileName(self, tr("Choose local shell executable"))
        if selected:
            index = self.shell.findData(selected)
            if index < 0:
                self.shell.addItem(selected, selected)
                index = self.shell.count() - 1
            self.shell.setCurrentIndex(index)
