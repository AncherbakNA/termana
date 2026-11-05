"""Connections/files switcher and a transfer queue independent of the active tab."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from termana.file_transfer.jobs import SftpTransferQueue
from termana.terminal.widgets.command_snippets_sidebar import CommandSnippetsSidebar
from termana.ui.i18n import tr
from termana.ui.theme import DEFAULT_THEME, ThemeColors
from termana.ui.ui_icons import interface_icon
from termana.ui.ui_toolbar import workbench_icon_colors
from termana.ui.widgets.chrome_tool_button import ChromeToolButton, anchor_tooltip
from termana.ui.widgets.wrapping_label import WrappingLabel


class WorkspaceSidebar(QWidget):
    """Keep catalog, commands and owner-bound files one explicit click apart."""

    files_requested = Signal()
    commands_requested = Signal()

    def __init__(self, connections, parent=None):
        super().__init__(parent)
        self.setObjectName("workspaceSidebar")
        self.setMinimumWidth(230)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.connections = connections
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 0, 0)
        layout.setSpacing(0)
        rail = QVBoxLayout()
        rail.setContentsMargins(0, 0, 4, 4)
        rail.setSpacing(4)
        self.connections_button = ChromeToolButton()
        self.commands_button = ChromeToolButton()
        self.files_button = ChromeToolButton()
        self.files_button.setEnabled(False)
        self._buttons = QButtonGroup(self)
        for button, label in (
            (self.connections_button, tr("Connections")),
            (self.commands_button, tr("Commands")),
            (self.files_button, tr("Files")),
        ):
            button.setText(label)
            button.setToolTip(label)
            anchor_tooltip(button, placement="right")
            button.setAccessibleName(label)
            button.setCheckable(True)
            button.setProperty("sidebarSurface", True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(22, 22))
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self._buttons.addButton(button)
            rail.addWidget(button)
        self.files_button.setAccessibleDescription(self.files_button.toolTip())
        rail.addStretch(1)
        layout.addLayout(rail)
        self.rail_separator = QFrame(self)
        self.rail_separator.setObjectName("sidebarRailSeparator")
        self.rail_separator.setFixedWidth(1)
        self.rail_separator.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.rail_separator.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rail_separator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.rail_separator)
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(content, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(connections)
        self.commands = CommandSnippetsSidebar()
        self.stack.addWidget(self.commands)
        self.file_stack = QStackedWidget()
        self.placeholder = WrappingLabel(tr("Select a running SSH session to browse files."))
        self.placeholder.setTextFormat(Qt.TextFormat.PlainText)
        self.file_stack.addWidget(self.placeholder)
        self.stack.addWidget(self.file_stack)
        content.addWidget(self.stack, 1)
        self.transfers = SftpTransferQueue(self)
        content.addWidget(self.transfers)
        self.connections_button.clicked.connect(self.show_connections)
        self.commands_button.clicked.connect(self.commands_requested)
        self.files_button.clicked.connect(self.files_requested)
        self.show_connections()
        self.set_interface_colors(DEFAULT_THEME)

    @property
    def showing_files(self):
        return self.stack.currentWidget() is self.file_stack

    def show_connections(self):
        self.connections_button.setChecked(True)
        self.stack.setCurrentWidget(self.connections)

    def show_commands(self):
        self.commands_button.setChecked(True)
        self.stack.setCurrentWidget(self.commands)

    def show_files(self, browser):
        if self.file_stack.indexOf(browser) < 0:
            self.file_stack.addWidget(browser)
        self.file_stack.setCurrentWidget(browser)
        self.stack.setCurrentWidget(self.file_stack)
        self.files_button.setChecked(True)

    def remove_browser(self, browser):
        if self.file_stack.currentWidget() is browser:
            if self.showing_files:
                self.show_connections()
            self.file_stack.setCurrentWidget(self.placeholder)
        self.file_stack.removeWidget(browser)

    def set_files_available(self, available, *, explanation=None):
        self.files_button.setEnabled(available)
        self.files_button.setToolTip(
            explanation
            or tr(
                "Remote files for the selected SSH session. System OpenSSH may ask for a separate connection."
            )
        )
        self.files_button.setAccessibleDescription(self.files_button.toolTip())
        if not available:
            if self.showing_files:
                self.show_connections()
            self.file_stack.setCurrentWidget(self.placeholder)

    def set_interface_colors(self, colors: ThemeColors, *, monochrome=False):
        self.connections.set_interface_colors(colors, monochrome=monochrome)
        tones = workbench_icon_colors(colors, monochrome=monochrome)
        self.connections_button.setIcon(interface_icon("server", colors.text))
        self.commands_button.setIcon(interface_icon("terminal", tones["command"]))
        self.files_button.setIcon(interface_icon("folder", tones["folder"]))
