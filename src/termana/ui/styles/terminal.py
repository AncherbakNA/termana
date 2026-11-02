"""Original stylesheet fragments grouped by UI responsibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.ui.theme import ThemeColors


def tabs_styles(colors: ThemeColors) -> str:
    """Original tabs rules; whitespace and cascade are deliberate."""
    return f"""
        QTabBar {{
            background-color: {colors.surface};
            border: none;
        }}

        QLabel#anchoredControlTooltip {{
            color: {colors.text};
            background-color: {colors.raised};
            border: 1px solid {colors.control_border};
            border-radius: 5px;
        }}

        QDialog#terminalGridDialog QPushButton {{
            min-height: 28px;
            padding: 0 12px;
        }}

        QFrame#terminalPane[tabDropTarget="true"] {{
            border: 2px solid {colors.accent};
        }}

        QTabBar::tab {{
            min-height: 34px;
            min-width: 72px;
            padding: 0 16px;
            margin: 0 2px 0 0;
            background-color: {colors.surface};
            color: {colors.muted_text};
            border: none;
            border-bottom: 2px solid transparent;
        }}

        QTabBar::tab:hover {{
            background-color: {colors.raised};
            color: {colors.text};
        }}

        QTabBar::tab:selected {{
            background-color: {colors.background};
            color: {colors.text};
            border-bottom: 2px solid {colors.accent};
            font-weight: 600;
        }}

        QTabWidget#terminalTabs QTabBar::tab {{
            min-width: 10em;
            margin: 4px 4px 0 0;
            border: 1px solid {colors.border};
            border-bottom: 2px solid {colors.border};
            border-top-left-radius: 7px;
            border-top-right-radius: 7px;
        }}

        QTabWidget#terminalTabs QTabBar::tab:selected {{
            border-color: {colors.control_border};
            border-bottom: 2px solid {colors.accent};
            background-color: {colors.raised};
        }}

        QWidget[connectionSection="true"] {{
            background-color: {colors.surface};
            border: 1px solid {colors.border};
            border-radius: 8px;
        }}

        QTabBar QToolButton {{
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.border};
            border-radius: 3px;
        }}

        QTabBar QToolButton:hover {{
            background-color: {colors.raised};
            border-color: {colors.control_border};
        }}

        QTabBar QToolButton:disabled {{
            color: {colors.muted_text};
        }}

        QToolButton#terminalTabCloseButton {{
            padding: 0;
            background-color: transparent;
            color: {colors.muted_text};
            border: 1px solid transparent;
            border-radius: 4px;
        }}

        QToolButton#terminalTabCloseButton:hover,
        QToolButton#terminalTabCloseButton:pressed {{
            color: {colors.text};
            background-color: {colors.raised};
            border-color: {colors.control_border};
        }}
"""


def start_styles(colors: ThemeColors) -> str:
    """Original start rules; whitespace and cascade are deliberate."""
    return f"""
        QWidget#startPage {{
            background-color: {colors.background};
        }}

        QFrame#startContent {{
            background-color: transparent;
            border: none;
        }}

        QScrollArea#startScrollArea,
        QWidget#startScrollViewport,
        QWidget#startCanvas {{
            background-color: {colors.background};
            border: none;
        }}

        QLabel#startBrandMark {{
            background-color: {colors.accent};
            color: {colors.accent_text};
            border-radius: 14px;
            font-weight: 800;
        }}

        QLabel#startEyebrow,
        QLabel#startTipLabel {{
            color: {colors.accent};
            font-weight: 800;
        }}

        QLabel#sidebarSectionLabel {{
            color: {colors.muted_text};
            font-weight: 600;
        }}

        QLabel#startDescription,
        QLabel#startTipText,
        QLabel#sidebarSubtitle {{
            color: {colors.muted_text};
        }}

        QPushButton#startNewConnectionButton {{
            min-height: 38px;
            padding: 0 17px;
            background-color: {colors.accent};
            color: {colors.accent_text};
            border: 1px solid {colors.accent};
            border-radius: 8px;
            font-weight: 700;
        }}

        QPushButton#startNewConnectionButton:hover,
        QPushButton#startNewConnectionButton:focus {{
            border-color: {colors.text};
        }}

        QPushButton#startLocalTerminalButton {{
            min-height: 38px;
            padding: 0 17px;
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 8px;
            font-weight: 600;
        }}

        QPushButton#startLocalTerminalButton:hover,
        QPushButton#startLocalTerminalButton:focus {{
            background-color: {colors.raised};
            border-color: {colors.accent};
        }}
"""


def surface_styles(colors: ThemeColors, *, scrollbar_minimum_handle: int) -> str:
    """Original surface rules; whitespace and cascade are deliberate."""
    return f"""
        QAbstractScrollArea#terminalOutput {{
            background-color: {colors.terminal.background};
            color: {colors.terminal.foreground};
            border: none;
        }}

        QAbstractScrollArea#terminalOutput QScrollBar:vertical {{
            width: 12px;
            margin: 0;
            background-color: {colors.terminal.background};
        }}

        QAbstractScrollArea#terminalOutput QScrollBar::handle:vertical {{
            min-height: {scrollbar_minimum_handle}px;
            background-color: {colors.border};
            border-radius: 5px;
        }}

        QAbstractScrollArea#terminalOutput QScrollBar::handle:vertical:hover {{
            background-color: {colors.muted_text};
        }}

        QAbstractScrollArea#terminalOutput QScrollBar::add-line:vertical,
        QAbstractScrollArea#terminalOutput QScrollBar::sub-line:vertical {{
            height: 0;
        }}

        QAbstractScrollArea#terminalOutput QScrollBar::add-page:vertical,
        QAbstractScrollArea#terminalOutput QScrollBar::sub-page:vertical {{
            background-color: transparent;
        }}

        QLabel#terminalStatus {{
            padding: 7px 12px;
            background-color: {colors.raised};
            color: {colors.muted_text};
            border-bottom: 1px solid {colors.border};
        }}

        QLabel#terminalStatus[statusType="error"] {{
            color: {colors.danger};
        }}

        QLabel#terminalStatus[statusType="warning"] {{
            color: {colors.warning_text};
            background-color: {colors.warning_surface};
        }}

        QFrame#terminalVisualBellFrame {{
            background-color: transparent;
            border: 2px solid transparent;
            border-radius: 5px;
        }}

        QFrame#terminalVisualBellFrame[bellState="active"] {{
            border-color: {colors.accent};
        }}

        QFrame#terminalEndedBanner {{
            background-color: {colors.surface};
            border-bottom: 1px solid {colors.control_border};
        }}

        QFrame#connectionAdvisory {{
            background-color: {colors.warning_surface};
            border-bottom: 1px solid {colors.control_border};
        }}

        QFrame#connectionAdvisory QLabel {{
            color: {colors.warning_text};
            background-color: transparent;
        }}

        QLabel#terminalEndedTitle {{
            color: {colors.text};
            font-weight: 600;
        }}

        QLabel#terminalEndedDetail {{
            color: {colors.muted_text};
        }}

        QFrame#terminalEndedBanner[statusType="error"] QLabel#terminalEndedDetail {{
            color: {colors.warning_text};
        }}

        QPushButton#terminalReconnectButton {{
            min-height: 34px;
            padding: 0 14px;
            background-color: {colors.accent};
            color: {colors.accent_text};
            border: 1px solid {colors.accent};
            border-radius: 7px;
            font-weight: 700;
        }}

        QPushButton#terminalReconnectButton:hover,
        QPushButton#terminalReconnectButton:focus {{
            border-color: {colors.text};
        }}

        QPushButton#terminalEndedCloseButton,
        QPushButton#terminalEndedSaveButton,
        QPushButton#terminalCopyKeyDetailsButton {{
            min-height: 34px;
            padding: 0 14px;
            background-color: {colors.raised};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
            font-weight: 600;
        }}

        QPushButton#terminalEndedCloseButton:hover,
        QPushButton#terminalEndedCloseButton:focus,
        QPushButton#terminalEndedSaveButton:hover,
        QPushButton#terminalEndedSaveButton:focus,
        QPushButton#terminalCopyKeyDetailsButton:hover,
        QPushButton#terminalCopyKeyDetailsButton:focus {{
            border-color: {colors.accent};
        }}

        QPushButton#terminalReconnectButton:disabled,
        QPushButton#terminalEndedCloseButton:disabled,
        QPushButton#terminalEndedSaveButton:disabled,
        QPushButton#terminalCopyKeyDetailsButton:disabled {{
            background-color: {colors.raised};
            color: {colors.muted_text};
            border-color: {colors.border};
        }}
"""


def search_styles(colors: ThemeColors) -> str:
    """Original search rules; whitespace and cascade are deliberate."""
    return f"""
        QFrame#terminalSearchBar {{
            background-color: {colors.surface};
            border-top: 1px solid {colors.border};
            border-bottom: 1px solid {colors.border};
        }}

        QLineEdit#terminalSearchInput {{
            min-height: 30px;
            padding: 0 9px;
            background-color: {colors.background};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 5px;
        }}

        QLineEdit#terminalSearchInput:focus {{
            border: 2px solid {colors.accent};
        }}

        QLabel#terminalSearchStatus {{
            min-width: 54px;
            padding: 0 4px;
            color: {colors.muted_text};
        }}

        QPushButton#terminalSearchMatchCaseButton,
        QPushButton#terminalSearchWholeWordsButton,
        QPushButton#terminalSearchPreviousButton,
        QPushButton#terminalSearchCloseButton {{
            min-height: 30px;
            padding: 0;
            background-color: {colors.raised};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 5px;
        }}

        QPushButton#terminalSearchMatchCaseButton:checked,
        QPushButton#terminalSearchWholeWordsButton:checked,
        QPushButton#terminalSearchNextButton {{
            min-height: 30px;
            padding: 0;
            background-color: {colors.accent};
            color: {colors.accent_text};
            border: 1px solid {colors.accent};
            border-radius: 5px;
            font-weight: 600;
        }}

        QPushButton#terminalSearchMatchCaseButton:hover,
        QPushButton#terminalSearchMatchCaseButton:focus,
        QPushButton#terminalSearchWholeWordsButton:hover,
        QPushButton#terminalSearchWholeWordsButton:focus,
        QPushButton#terminalSearchPreviousButton:hover,
        QPushButton#terminalSearchPreviousButton:focus,
        QPushButton#terminalSearchNextButton:hover,
        QPushButton#terminalSearchNextButton:focus,
        QPushButton#terminalSearchCloseButton:hover,
        QPushButton#terminalSearchCloseButton:focus {{
            border-color: {colors.text};
        }}

        QPushButton#terminalSearchPreviousButton:pressed,
        QPushButton#terminalSearchNextButton:pressed,
        QPushButton#terminalSearchCloseButton:pressed {{
            background-color: {colors.background};
            color: {colors.text};
        }}

        QLineEdit#terminalCommandInput {{
            min-height: 36px;
            padding: 0 12px;
            background-color: {colors.surface};
            color: {colors.text};
            border: none;
            border-top: 1px solid {colors.border};
            border-radius: 0;
        }}

        QLineEdit#terminalCommandInput:focus {{
            border: 2px solid {colors.accent};
        }}
"""
