"""Original stylesheet fragments grouped by UI responsibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.ui.theme import ThemeColors


def sidebar_buttons_styles(colors: ThemeColors) -> str:
    """Original sidebar buttons rules; whitespace and cascade are deliberate."""
    return f"""
        QFrame#sidebarRailSeparator {{
            border: none;
            background-color: {colors.border};
        }}

        QToolButton[sidebarSurface="true"] {{
            background-color: transparent;
            color: {colors.muted_text};
            border: 1px solid transparent;
            border-left: 2px solid transparent;
            padding: 8px 6px;
        }}

        QToolButton[sidebarSurface="true"]:checked {{
            background-color: {colors.surface};
            color: {colors.text};
            border-left-color: {colors.accent};
        }}

        QToolButton[sidebarSurface="true"]:focus,
        QToolButton[sidebarSurface="true"]:hover {{
            color: {colors.text};
        }}

        QToolButton[sidebarSurface="true"]:focus:!checked {{
            border-top: 1px dotted {colors.muted_text};
            border-right: 1px dotted {colors.muted_text};
            border-bottom: 1px dotted {colors.muted_text};
        }}

        QToolButton[navigationRole="remoteDirectory"] {{
            background-color: transparent;
            border: 1px solid transparent;
            border-radius: 5px;
            padding: 6px;
        }}

        QToolButton[navigationRole="remoteDirectory"]:hover {{
            background-color: {colors.raised};
        }}

        QToolButton[navigationRole="remoteDirectory"]:focus {{
            border-color: {colors.accent};
        }}

        QToolButton[navigationRole="remoteDirectory"]:pressed {{
            background-color: {colors.raised};
            border-color: {colors.text};
        }}

        QToolButton[navigationRole="remoteDirectory"]:disabled {{
            background-color: transparent;
            border-color: transparent;
        }}
"""


def menus_styles(colors: ThemeColors) -> str:
    """Original menus rules; whitespace and cascade are deliberate."""
    return f"""
        QMenuBar {{
            background-color: {colors.surface};
            color: {colors.text};
            border-bottom: 1px solid {colors.border};
        }}

        QMenuBar::item {{
            padding: 7px 10px;
            background-color: transparent;
            border-radius: 5px;
        }}

        QMenuBar::item:selected,
        QMenuBar::item:pressed {{
            background-color: {colors.raised};
            color: {colors.text};
            border-bottom: 2px solid {colors.accent};
        }}

        QMenu {{
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            padding: 5px;
        }}

        QMenu::item {{
            padding: 8px 26px 8px 10px;
            border-radius: 5px;
        }}

        QMenu::item:selected {{
            background-color: {colors.raised};
            border-left: 3px solid {colors.accent};
        }}
"""


def toolbar_styles(colors: ThemeColors) -> str:
    """Original toolbar rules; whitespace and cascade are deliberate."""
    return f"""
        QToolBar {{
            min-height: 64px;
            padding: 5px 7px;
            spacing: 3px;
            background-color: {colors.surface};
            border: none;
            border-bottom: 1px solid {colors.border};
        }}

        QToolBar QToolButton {{
            min-height: 54px;
            min-width: 54px;
            padding: 5px 3px;
            background-color: transparent;
            color: {colors.text};
            border: 1px solid transparent;
            border-radius: 3px;
        }}

        QToolBar QToolButton:hover,
        QToolBar QToolButton:focus {{
            background-color: {colors.raised};
            border-color: {colors.border};
        }}

        QToolBar QToolButton:focus {{
            border-color: {colors.accent};
        }}

        QToolBar QToolButton:checked {{
            background-color: {colors.raised};
            border-color: {colors.border};
        }}

        QToolBar QToolButton:disabled {{
            color: {colors.muted_text};
        }}

        QToolBar#rdpSessionTools {{
            min-height: 0px;
            padding: 2px 4px;
            spacing: 4px;
        }}

        QToolBar#rdpSessionTools QToolButton {{
            min-height: 0px;
            min-width: 0px;
            padding: 4px 8px;
        }}

        QToolButton[rdpToolMenu="true"]::menu-indicator {{
            image: none;
            width: 0px;
            height: 0px;
        }}

        QToolButton#qt_toolbar_ext_button {{
            min-width: 16px;
            padding: 5px 2px;
        }}

        QToolButton#toolbar_paneLayoutAction::menu-indicator {{
            image: none;
            width: 0px;
            height: 0px;
        }}

        QFrame#connectionsSidebar {{
            background-color: {colors.surface};
            border: none;
        }}

        QScrollArea#connectionsSidebarScroll,
        QWidget#connectionsSidebarScrollViewport,
        QWidget#connectionsSidebarContent {{
            background-color: {colors.surface};
            border: none;
        }}

        QFrame#terminalWorkspace {{
            background-color: {colors.background};
        }}
"""


def status_styles(colors: ThemeColors) -> str:
    """Original status rules; whitespace and cascade are deliberate."""
    return f"""
        QToolTip {{
            background-color: {colors.raised};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            padding: 5px 7px;
        }}

        QStatusBar {{
            min-height: 22px;
            background-color: {colors.surface};
            color: {colors.muted_text};
            border-top: 1px solid {colors.border};
        }}

        QStatusBar::item {{
            border: none;
        }}
"""
