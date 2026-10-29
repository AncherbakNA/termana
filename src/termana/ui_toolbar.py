"""Presentation metadata for the workbench's existing shared actions."""

from dataclasses import dataclass
from typing import Final, Literal

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QActionEvent
from PySide6.QtWidgets import QToolBar, QToolButton, QWidget

from termana.theme import ThemeColors
from termana.ui_icons import InterfaceIconName
from termana.ui_typography import scale_widget_font
from termana.widgets.chrome_tool_button import anchor_tooltip

IconTone = Literal["connection", "folder", "terminal", "layout", "command", "neutral"]


class WorkbenchToolBar(QToolBar):
    """Overflow whole commands instead of compressing their icon captions."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._refresh_pending = False

    def actionEvent(self, event: QActionEvent) -> None:
        super().actionEvent(event)
        if event.type() == QEvent.Type.ActionAdded:
            button = self.widgetForAction(event.action())
            if isinstance(button, QToolButton):
                button.installEventFilter(self)
                anchor_tooltip(button)
                scale_widget_font(button, 1.0)
                button.setMinimumSize(button.sizeHint())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QToolButton) and event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.ActionChanged,
            QEvent.Type.Polish,
        ):
            # Use Qt's own font/icon/style metrics, including stylesheet padding.
            # A theme or accessibility font change must refresh both dimensions.
            # The filter runs before QToolButton invalidates its sizeHint cache.
            if not self._refresh_pending:
                self._refresh_pending = True
                QTimer.singleShot(0, self, self._refresh_button_sizes)
        return super().eventFilter(watched, event)

    def _refresh_button_sizes(self) -> None:
        self._refresh_pending = False
        for action in self.actions():
            button = self.widgetForAction(action)
            if isinstance(button, QToolButton):
                button.setMinimumSize(0, 0)
                button.setMinimumSize(button.sizeHint())


@dataclass(frozen=True, slots=True)
class WorkbenchTool:
    """Keep short toolbar labels separate from descriptive menu commands."""

    action_attribute: str
    label: str
    icon: InterfaceIconName
    tone: IconTone


WORKBENCH_TOOLS: Final = (
    WorkbenchTool("new_connection_action", "Connect", "new_connection", "connection"),
    WorkbenchTool("toggle_sidebar_action", "Sidebar", "folder", "folder"),
    WorkbenchTool("local_terminal_action", "Local", "terminal", "terminal"),
    WorkbenchTool("pane_layout_action", "Split", "split", "layout"),
    WorkbenchTool("saved_commands_action", "Commands", "terminal", "command"),
    WorkbenchTool("switch_terminal_action", "Tabs", "tabs", "layout"),
    WorkbenchTool("settings_action", "Settings", "settings", "neutral"),
)


def workbench_icon_colors(colors: ThemeColors, *, monochrome: bool = False) -> dict[IconTone, str]:
    """Use existing palette roles, keeping monochrome appearance genuinely neutral."""
    if monochrome:
        return dict.fromkeys(
            ("connection", "folder", "terminal", "layout", "command", "neutral"), colors.text
        )
    return {
        "connection": colors.terminal.ansi.green,
        "folder": colors.terminal.ansi.yellow,
        "terminal": colors.terminal.ansi.cyan,
        "layout": colors.terminal.ansi.blue,
        "command": colors.terminal.ansi.magenta,
        "neutral": colors.muted_text,
    }
