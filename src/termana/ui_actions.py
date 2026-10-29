"""Small, explicit construction boundary for application-owned window actions."""

from collections.abc import Callable

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QKeySequence

from termana.i18n import tr


def create_window_action(
    parent: QObject,
    text: str,
    object_name: str,
    handler: Callable[..., object],
    *,
    status_tip: str = "",
    tool_tip: str | None = None,
    shortcuts: tuple[QKeySequence, ...] = (),
    auto_repeat: bool = True,
    checkable: bool = False,
) -> QAction:
    """Keep Qt wiring consistent while leaving command metadata at the call site."""
    action = QAction(tr(text), parent)
    action.setObjectName(object_name)
    action.setStatusTip(tr(status_tip))
    if tool_tip is not None:
        action.setToolTip(tr(tool_tip))
    # Some native standard bindings are empty on headless/test platforms.
    # Match QAction.setShortcut(empty), which installs no shortcut at all.
    action.setShortcuts(tuple(shortcut for shortcut in shortcuts if not shortcut.isEmpty()))
    action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
    action.setAutoRepeat(auto_repeat)
    action.setCheckable(checkable)
    action.triggered.connect(handler)
    return action
