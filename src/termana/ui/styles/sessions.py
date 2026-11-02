"""Original stylesheet fragments grouped by UI responsibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.ui.theme import ThemeColors


def filter_styles(colors: ThemeColors) -> str:
    """Original filter rules; whitespace and cascade are deliberate."""
    return f"""
        QLineEdit#sessionsFilterInput {{
            min-height: 32px;
            padding: 0 10px;
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
        }}

        QLineEdit#sessionsFilterInput:focus {{
            background-color: {colors.background};
            border: 2px solid {colors.accent};
        }}

        QPushButton#favoritesFilterButton {{
            min-height: 30px;
            padding: 0 10px;
            background-color: {colors.surface};
            color: {colors.muted_text};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
        }}

        QPushButton#favoritesFilterButton:hover {{
            background-color: {colors.raised};
            color: {colors.text};
        }}

        QPushButton#favoritesFilterButton:checked {{
            background-color: {colors.raised};
            color: {colors.text};
            border-color: {colors.accent};
        }}

        QPushButton#favoritesFilterButton:focus {{
            border: 2px solid {colors.accent};
        }}

        QPushButton#favoritesFilterButton:disabled {{
            color: {colors.muted_text};
            border-color: {colors.border};
        }}

        QLabel#sessionsFilterStatus,
        QLabel#sessionsFilterStatus[filterState="results"] {{
            padding: 2px 4px;
            background-color: transparent;
            color: {colors.muted_text};
        }}

        QLabel#sessionsFilterStatus[filterState="noResults"] {{
            padding: 4px 6px;
            background-color: {colors.raised};
            color: {colors.muted_text};
            border: 1px solid {colors.control_border};
            border-radius: 4px;
        }}

        QFrame#connectionCatalogStatePanel {{
            background-color: {colors.warning_surface};
            border: 1px solid {colors.warning};
            border-radius: 9px;
        }}

        QLabel#connectionCatalogStateTitle {{
            background-color: transparent;
            color: {colors.warning_text};
            font-weight: 600;
        }}

        QLabel#connectionCatalogStateDescription {{
            background-color: transparent;
            color: {colors.text};
        }}

        QWidget#connectionCatalogStateSpacer {{
            background-color: transparent;
        }}

        QPushButton#retryConnectionCatalogButton {{
            min-height: 34px;
            padding: 0 12px;
            background-color: {colors.raised};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
        }}

        QPushButton#retryConnectionCatalogButton:hover,
        QPushButton#retryConnectionCatalogButton:focus {{
            border-color: {colors.accent};
        }}
"""


def tree_styles(colors: ThemeColors) -> str:
    """Original tree rules; whitespace and cascade are deliberate."""
    return f"""
        QTreeWidget#sessionsTree {{
            show-decoration-selected: 0;
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid transparent;
            outline: none;
        }}

        QTreeWidget#sessionsTree::item:focus {{
            border-color: {colors.accent};
        }}

        QTreeWidget#sessionsTree::item {{
            min-height: 24px;
            padding: 2px 4px;
            border: 1px solid transparent;
            border-radius: 2px;
        }}

        QTreeWidget#sessionsTree::item:hover {{
            background-color: {colors.raised};
        }}

        QTreeWidget#sessionsTree::item:selected {{
            background-color: {colors.raised};
            color: {colors.text};
        }}

        QFrame#maximizedPaneBanner {{
            background-color: {colors.surface};
            border: none;
            border-bottom: 1px solid {colors.border};
        }}

        QLabel#maximizedPaneTitle {{
            color: {colors.text};
            font-weight: 600;
        }}

        QLabel#maximizedPaneHint {{
            color: {colors.muted_text};
        }}

        QFrame#terminalPane {{
            background-color: {colors.background};
            border: 1px solid {colors.border};
            border-radius: 4px;
        }}

        QFrame#terminalPane[activePane="true"][multiplePanes="true"] {{
            border-color: {colors.accent};
        }}

        QLabel#terminalPaneHeader {{
            background-color: {colors.surface};
            color: {colors.muted_text};
            font-weight: 600;
        }}

        QFrame#terminalPaneEmpty {{
            background-color: {colors.background};
            border: none;
        }}

        QLabel#terminalPaneEmptyTitle {{
            color: {colors.text};
            font-weight: 600;
        }}

        QLabel#terminalPaneEmptyHint {{
            color: {colors.muted_text};
        }}

        QSplitter#terminalPaneSplitter::handle {{
            background-color: {colors.surface};
        }}

        QSplitter#terminalPaneSplitter::handle:hover {{
            background-color: {colors.accent};
        }}

        QTabWidget::pane {{
            background-color: {colors.background};
            border: none;
        }}
"""
