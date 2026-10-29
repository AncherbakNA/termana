"""Color themes and Qt style sheet generation for Termana."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final

from termana.models.settings import ThemeId


@dataclass(frozen=True, slots=True)
class AnsiColors:
    """The sixteen standard colors that terminal applications can request."""

    black: str
    red: str
    green: str
    yellow: str
    blue: str
    magenta: str
    cyan: str
    white: str
    bright_black: str
    bright_red: str
    bright_green: str
    bright_yellow: str
    bright_blue: str
    bright_magenta: str
    bright_cyan: str
    bright_white: str


@dataclass(frozen=True, slots=True)
class TerminalColors:
    """Colors owned by the terminal renderer rather than the general UI."""

    foreground: str
    background: str
    cursor: str
    cursor_text: str
    selection_background: str
    selection_foreground: str
    ansi: AnsiColors


@dataclass(frozen=True, slots=True)
class ThemeColors:
    """Named color roles used throughout the application."""

    background: str
    surface: str
    raised: str
    text: str
    muted_text: str
    border: str
    control_border: str
    accent: str
    accent_text: str
    warning: str
    warning_surface: str
    warning_text: str
    danger: str
    danger_text: str
    terminal: TerminalColors


# Keep the original dark/light storage IDs compatible: both now use Graphite.
DARK_THEME = ThemeColors(
    background="#202226",
    surface="#202226",
    raised="#2B2E34",
    text="#E4E7ED",
    muted_text="#A5ADBA",
    border="#343840",
    control_border="#798496",
    accent="#8DA5EB",
    accent_text="#151D32",
    warning="#F2CC60",
    warning_surface="#302918",
    warning_text="#FFE39A",
    danger="#F26B6B",
    danger_text="#190707",
    terminal=TerminalColors(
        foreground="#E4E7ED",
        background="#16181B",
        cursor="#8DA5EB",
        cursor_text="#151D32",
        selection_background="#344466",
        selection_foreground="#E4E7ED",
        ansi=AnsiColors(
            black="#0E141B",
            red="#F26B6B",
            green="#7BD88F",
            yellow="#F2CC60",
            blue="#6CB6FF",
            magenta="#C099FF",
            cyan="#38D6C4",
            white="#E8EEF4",
            bright_black="#667788",
            bright_red="#FF8A8A",
            bright_green="#A3F7B5",
            bright_yellow="#FFE083",
            bright_blue="#8CC8FF",
            bright_magenta="#D6B8FF",
            bright_cyan="#72EFE1",
            bright_white="#FFFFFF",
        ),
    ),
)

LIGHT_THEME = ThemeColors(
    background="#F5F6F8",
    surface="#FFFFFF",
    raised="#ECEFF3",
    text="#252A33",
    muted_text="#636D7C",
    border="#DCE1E8",
    control_border="#798496",
    accent="#4566C9",
    accent_text="#FFFFFF",
    warning="#B7791F",
    warning_surface="#FFF4CE",
    warning_text="#6B4500",
    danger="#C43D3D",
    danger_text="#FFFFFF",
    terminal=TerminalColors(
        foreground="#252A33",
        background="#FFFFFF",
        cursor="#4566C9",
        cursor_text="#FFFFFF",
        selection_background="#E3E9F6",
        selection_foreground="#252A33",
        ansi=AnsiColors(
            black="#16212B",
            red="#B42318",
            green="#18794E",
            yellow="#946200",
            blue="#175CD3",
            magenta="#7F56D9",
            cyan="#0E7490",
            white="#475467",
            bright_black="#667085",
            bright_red="#C4320A",
            bright_green="#087A48",
            bright_yellow="#825500",
            bright_blue="#155EEF",
            bright_magenta="#7047C8",
            bright_cyan="#0B7189",
            bright_white="#101828",
        ),
    ),
)

WARM_THEME = replace(
    DARK_THEME,
    background="#25231F",
    surface="#292722",
    raised="#35312A",
    text="#E9E6DF",
    muted_text="#B0A899",
    border="#423D34",
    control_border="#948773",
    accent="#CBAE84",
    accent_text="#251E14",
    terminal=replace(
        DARK_THEME.terminal,
        foreground="#E9E6DF",
        background="#1C1B18",
        cursor="#CBAE84",
        cursor_text="#251E14",
        selection_background="#4B3F2D",
        selection_foreground="#E9E6DF",
    ),
)
WARM_LIGHT_THEME = replace(
    LIGHT_THEME,
    background="#F6F4EF",
    surface="#FAF9F6",
    raised="#E9E5DC",
    text="#302C26",
    muted_text="#6C6458",
    border="#DCD6CA",
    control_border="#8A7D68",
    accent="#816037",
    terminal=replace(
        LIGHT_THEME.terminal,
        foreground="#302C26",
        background="#FFFEFA",
        cursor="#816037",
        selection_background="#E9DFCB",
        selection_foreground="#302C26",
    ),
)
MONOCHROME_THEME = replace(
    DARK_THEME,
    background="#202022",
    surface="#242426",
    raised="#303033",
    text="#E9E9ED",
    muted_text="#ADADB7",
    border="#3B3B40",
    control_border="#868690",
    accent="#DDDDE5",
    accent_text="#202024",
    terminal=replace(
        DARK_THEME.terminal,
        foreground="#E9E9ED",
        background="#171719",
        cursor="#DDDDE5",
        cursor_text="#202024",
        selection_background="#414147",
        selection_foreground="#E9E9ED",
    ),
)
MONOCHROME_LIGHT_THEME = replace(
    LIGHT_THEME,
    background="#F5F5F6",
    raised="#EAEAED",
    text="#28282D",
    muted_text="#676771",
    border="#DEDEE3",
    control_border="#858591",
    accent="#414148",
    terminal=replace(
        LIGHT_THEME.terminal,
        foreground="#28282D",
        cursor="#414148",
        selection_background="#E4E4E9",
        selection_foreground="#28282D",
    ),
)

THEME_LABELS: Final[Mapping[ThemeId, str]] = MappingProxyType(
    {
        ThemeId.DARK: "Graphite · Dark (default)",
        ThemeId.LIGHT: "Graphite · Light",
        ThemeId.WARM: "Warm · Dark",
        ThemeId.WARM_LIGHT: "Warm · Light",
        ThemeId.MONOCHROME: "Monochrome · Dark",
        ThemeId.MONOCHROME_LIGHT: "Monochrome · Light",
    }
)

THEMES: Final[Mapping[ThemeId, ThemeColors]] = MappingProxyType(
    {
        ThemeId.DARK: DARK_THEME,
        ThemeId.LIGHT: LIGHT_THEME,
        ThemeId.WARM: WARM_THEME,
        ThemeId.WARM_LIGHT: WARM_LIGHT_THEME,
        ThemeId.MONOCHROME: MONOCHROME_THEME,
        ThemeId.MONOCHROME_LIGHT: MONOCHROME_LIGHT_THEME,
    }
)
DEFAULT_THEME = THEMES[ThemeId.DARK]


def colors_for_theme(theme_id: ThemeId) -> ThemeColors:
    """Resolve a stable preference identifier to immutable color roles."""
    return THEMES[theme_id]


FORM_FIELD_HORIZONTAL_PADDING = 10
SCROLLBAR_EXTENT = 14
SCROLLBAR_MINIMUM_HANDLE = 28


def build_stylesheet(colors: ThemeColors) -> str:
    """Build the application-wide Qt style sheet for a color theme."""
    return f"""
        QWidget {{
            background-color: {colors.background};
            color: {colors.text};
            selection-background-color: {colors.accent};
            selection-color: {colors.accent_text};
        }}

        QWidget:disabled {{
            color: {colors.muted_text};
        }}

        ThemedCheckBox {{
            spacing: 7px;
        }}

        ThemedCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border: 1px solid {colors.control_border};
            border-radius: 3px;
            background-color: {colors.surface};
            image: none;
        }}

        ThemedCheckBox::indicator:checked,
        ThemedCheckBox::indicator:indeterminate {{
            border-color: {colors.accent};
            background-color: {colors.raised};
        }}

        ThemedCheckBox::indicator:hover,
        ThemedCheckBox::indicator:focus {{
            border-color: {colors.accent};
        }}

        ThemedCheckBox::indicator:disabled {{
            border-color: {colors.border};
            background-color: {colors.raised};
        }}

        QLabel {{
            background-color: transparent;
        }}

        QLabel#sftpOwnerLabel {{
            font-weight: 600;
            color: {colors.text};
        }}

        QLabel#sftpConnectionNote,
        QLabel#sftpConnectionBadge,
        QLabel#sftpListingSummary,
        QLabel#sftpSelectionSummary {{
            color: {colors.muted_text};
        }}

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

        QDialog#connectionDialog,
        QDialog#connectionFolderDialog,
        QDialog#settingsDialog,
        QDialog#sshHostKeyChangedDialog,
        QDialog#sshLoginDialog,
        QDialog#savedSshLoginsDialog,
        QDialog#sshAccountsDialog,
        QDialog#telnetConnectionWarningDialog,
        QDialog[dialogRole="actionNotice"] {{
            background-color: {colors.background};
        }}

        QScrollArea#connectionDialogBodyScroll,
        QWidget#connectionDialogBodyViewport,
        QWidget#connectionDialogBody {{
            background-color: {colors.background};
            border: none;
        }}

        QWidget#connectionDialogFooter {{
            background-color: {colors.surface};
            border-top: 1px solid {colors.border};
        }}

        QMessageBox#closeActiveTerminalDialog,
        QMessageBox#removeChooserSshLoginDialog,
        QMessageBox#removeSavedSshLoginDialog,
        QMessageBox#removeAllSavedSshLoginsDialog,
        QMessageBox#savedSshLoginsLoadErrorDialog {{
            background-color: {colors.background};
            color: {colors.text};
        }}

        QSplitter::handle {{
            background-color: {colors.background};
            border-left: 1px solid {colors.border};
        }}

        QTableView {{
            background-color: {colors.surface};
            alternate-background-color: {colors.raised};
            color: {colors.text};
            gridline-color: {colors.border};
            border: 1px solid {colors.border};
            selection-background-color: {colors.accent};
            selection-color: {colors.accent_text};
        }}

        QTableView::item:selected {{
            background-color: {colors.accent};
            color: {colors.accent_text};
        }}

        QTableView::item:selected:disabled {{
            background-color: {colors.raised};
            color: {colors.muted_text};
        }}

        QTableView QHeaderView::section {{
            background-color: {colors.surface};
            color: {colors.muted_text};
            border: none;
            border-bottom: 1px solid {colors.border};
            padding: 4px 6px;
        }}

        QTableView QHeaderView::section:hover,
        QTableView QHeaderView::section:pressed {{
            background-color: {colors.raised};
            color: {colors.text};
        }}

        QTableCornerButton::section {{
            background-color: {colors.surface};
            border: none;
            border-bottom: 1px solid {colors.border};
        }}

        QScrollBar:vertical {{
            width: {SCROLLBAR_EXTENT}px;
            margin: 0;
            background-color: {colors.background};
        }}

        QScrollBar:horizontal {{
            height: {SCROLLBAR_EXTENT}px;
            margin: 0;
            background-color: {colors.background};
        }}

        QScrollBar::handle:vertical {{
            min-height: {SCROLLBAR_MINIMUM_HANDLE}px;
            background-color: {colors.control_border};
            border-radius: 6px;
        }}

        QScrollBar::handle:horizontal {{
            min-width: {SCROLLBAR_MINIMUM_HANDLE}px;
            background-color: {colors.control_border};
            border-radius: 6px;
        }}

        QScrollBar::handle:hover,
        QScrollBar::handle:pressed {{
            background-color: {colors.muted_text};
        }}

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            height: 0;
        }}

        QScrollBar::add-line:horizontal,
        QScrollBar::sub-line:horizontal {{
            width: 0;
        }}

        QScrollBar::add-page,
        QScrollBar::sub-page {{
            background-color: transparent;
        }}

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

        QTreeWidget#sessionsTree {{
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
            min-height: {SCROLLBAR_MINIMUM_HANDLE}px;
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

        QLabel#sectionTitle {{
            color: {colors.text};
            font-weight: 700;
        }}

        QLabel#settingsSectionTitle {{
            color: {colors.text};
            font-weight: 700;
        }}

        QLabel#emptyStateTitle {{
            color: {colors.text};
            font-weight: 700;
        }}

        QLabel#dialogTitle {{
            color: {colors.text};
            font-weight: 700;
        }}

        QLabel#mutedText {{
            color: {colors.muted_text};
        }}

        QFrame#actionDialogBadge {{
            background-color: {colors.raised};
            border: 1px solid {colors.accent};
            border-radius: 22px;
        }}

        QFrame#actionDialogBadge[tone="secure"] {{
            background-color: {colors.surface};
        }}

        QLabel#actionDialogBadgeSymbol {{
            color: {colors.accent};
            font-weight: 700;
        }}

        QLabel#actionDialogTitle {{
            color: {colors.text};
            font-weight: 700;
        }}

        QLabel#actionDialogSubtitle {{
            color: {colors.muted_text};
        }}

        QLabel#actionDialogBody {{
            color: {colors.text};
        }}

        QScrollArea#telnetWarningBodyScroll,
        QWidget#telnetWarningBodyViewport,
        QWidget#telnetWarningBody {{
            background-color: transparent;
            border: none;
        }}

        QFrame#telnetEndpointCard {{
            background-color: {colors.raised};
            border: 1px solid {colors.border};
            border-radius: 9px;
        }}

        QLabel#telnetEndpointHeading {{
            color: {colors.muted_text};
            font-weight: 700;
        }}

        QLineEdit#telnetEndpointField {{
            min-height: 32px;
            padding: 0 9px;
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
            selection-background-color: {colors.terminal.selection_background};
            selection-color: {colors.terminal.selection_foreground};
        }}

        QLabel#telnetSafetyHint {{
            color: {colors.muted_text};
        }}

        QWidget#telnetWarningFooter {{
            background-color: {colors.surface};
            border-top: 1px solid {colors.border};
        }}

        QLabel#savedSshLoginsDialogTitle {{
            color: {colors.text};
            font-weight: 600;
        }}

        QLabel#savedSshLoginsDescription,
        QLabel#savedSshLoginsCount,
        QLabel#savedSshLoginsStateDescription {{
            color: {colors.muted_text};
        }}

        QLabel#savedSshLoginsOperationStatus {{
            color: {colors.accent};
            font-weight: 600;
        }}

        QLineEdit#savedSshLoginFilterInput,
        QLineEdit#openTerminalSearch {{
            min-height: 30px;
            padding: 0 9px;
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 5px;
        }}

        QLineEdit#savedSshLoginFilterInput:focus,
        QLineEdit#openTerminalSearch:focus {{
            border: 2px solid {colors.accent};
        }}

        QTreeWidget#savedSshLoginsTree,
        QListWidget#savedCommandsList,
        QListWidget#openTerminalList {{
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
            outline: none;
        }}

        QTreeWidget#savedSshLoginsTree:focus,
        QListWidget#savedCommandsList:focus,
        QListWidget#openTerminalList:focus {{
            border: 1px solid {colors.accent};
        }}

        QTreeWidget#savedSshLoginsTree::item,
        QListWidget#savedCommandsList::item {{
            min-height: 30px;
            padding: 3px 8px;
        }}

        QListWidget#openTerminalList::item {{
            padding: 8px 10px;
            border-left: 3px solid transparent;
        }}

        QTreeWidget#savedSshLoginsTree::item:hover,
        QListWidget#savedCommandsList::item:hover,
        QListWidget#openTerminalList::item:hover {{
            background-color: {colors.raised};
        }}

        QTreeWidget#savedSshLoginsTree::item:selected,
        QListWidget#savedCommandsList::item:selected,
        QListWidget#openTerminalList::item:selected {{
            background-color: {colors.raised};
            color: {colors.text};
        }}

        QListWidget#openTerminalList::item:selected {{
            border-left: 3px solid {colors.accent};
        }}

        QFrame#savedSshLoginsStatePanel {{
            background-color: {colors.surface};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
        }}

        QLabel#savedSshLoginsStateTitle {{
            color: {colors.text};
            font-weight: 600;
        }}

        QPlainTextEdit#savedSshLoginsError {{
            padding: 8px 10px;
            color: {colors.danger};
            background-color: {colors.warning_surface};
            border: 1px solid {colors.danger};
            border-radius: 5px;
        }}

        QLabel#connectionValidationError,
        QLabel#connectionFolderValidationError {{
            color: {colors.danger};
        }}

        QLabel#telnetSecurityNotice {{
            padding: 8px 10px;
            color: {colors.warning_text};
            background-color: {colors.warning_surface};
            border: 1px solid {colors.warning};
            border-radius: 5px;
        }}

        QLabel#settingsError {{
            padding: 8px 10px;
            color: {colors.danger};
            background-color: {colors.warning_surface};
            border: 1px solid {colors.danger};
            border-radius: 5px;
        }}

        QFrame#hostKeyWarningBadge {{
            background-color: {colors.warning_surface};
            border: 1px solid {colors.warning};
            border-radius: 24px;
        }}

        QLabel#hostKeyWarningSymbol {{
            color: {colors.warning};
            font-weight: 700;
        }}

        QLabel#hostKeyDialogTitle {{
            color: {colors.text};
            font-weight: 600;
        }}

        QLabel#sshHostEndpointLabel,
        QLabel#hostKeyFooterHint {{
            color: {colors.muted_text};
        }}

        QFrame#hostKeyFingerprintCard,
        QFrame#hostKeyDetailsCard {{
            background-color: {colors.surface};
            border: 1px solid {colors.control_border};
            border-radius: 8px;
        }}

        QLabel#hostKeyFingerprintTitle {{
            color: {colors.muted_text};
            font-weight: 700;
        }}

        QLineEdit#sshHostFingerprintLabel {{
            min-height: 36px;
            padding: 10px 12px;
            background-color: {colors.raised};
            color: {colors.text};
            border: none;
            border-radius: 5px;
        }}

        QLabel#hostKeySafetyHint {{
            padding: 10px 12px;
            background-color: {colors.warning_surface};
            color: {colors.warning_text};
            border: 1px solid {colors.warning};
            border-radius: 6px;
        }}

        QLabel#sshHostKeyDetailsLabel {{
            color: {colors.muted_text};
        }}

        QFrame#sshLoginBadge {{
            background-color: {colors.raised};
            border: 1px solid {colors.accent};
            border-radius: 21px;
        }}

        QLabel#sshLoginBadgeSymbol {{
            color: {colors.accent};
            font-weight: 700;
        }}

        QLabel#sshLoginDialogTitle {{
            color: {colors.text};
            font-weight: 600;
        }}

        QLabel#sshLoginEndpointLabel,
        QLabel#sshLoginDescription {{
            color: {colors.muted_text};
        }}

        QListWidget#sshLoginOptions,
        QListWidget#sshAccountsList {{
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 8px;
            outline: none;
        }}

        QListWidget#sshLoginOptions:focus,
        QListWidget#sshAccountsList:focus {{
            border: 1px solid {colors.accent};
        }}

        QListWidget#sshLoginOptions::item,
        QListWidget#sshAccountsList::item {{
            min-height: 40px;
            padding: 0 12px;
            border-bottom: 1px solid {colors.border};
        }}

        QListWidget#sshLoginOptions::item:last {{
            border-bottom: none;
        }}

        QListWidget#sshLoginOptions::item:hover {{
            background-color: {colors.raised};
        }}

        QListWidget#sshLoginOptions::item:selected,
        QListWidget#sshAccountsList::item:selected {{
            background-color: {colors.raised};
            color: {colors.text};
            border-left: 3px solid {colors.accent};
        }}

        QCheckBox#rememberSshLoginCheckbox {{
            color: {colors.muted_text};
            spacing: 8px;
        }}

        QToolButton#hostKeyDetailsToggle,
        QToolButton#sshConnectionSettingsToggle,
        QToolButton#sshJumpSettingsToggle,
        QToolButton#sshTunnelSettingsToggle,
        QToolButton#sshIdentityHelpToggle,
        QToolButton#sshIdentitySettingsToggle {{
            padding: 4px 2px;
            background-color: transparent;
            color: {colors.muted_text};
            border: none;
        }}

        QToolButton#hostKeyDetailsToggle:hover,
        QToolButton#hostKeyDetailsToggle:focus,
        QToolButton#sshConnectionSettingsToggle:hover,
        QToolButton#sshConnectionSettingsToggle:focus,
        QToolButton#sshJumpSettingsToggle:hover,
        QToolButton#sshJumpSettingsToggle:focus,
        QToolButton#sshTunnelSettingsToggle:hover,
        QToolButton#sshTunnelSettingsToggle:focus,
        QToolButton#sshIdentityHelpToggle:hover,
        QToolButton#sshIdentityHelpToggle:focus,
        QToolButton#sshIdentitySettingsToggle:hover,
        QToolButton#sshIdentitySettingsToggle:focus {{
            color: {colors.text};
            border: 1px solid {colors.accent};
        }}

        QFrame#sshTunnelRule {{
            background-color: {colors.raised};
            border: 1px solid {colors.control_border};
            border-radius: 8px;
        }}

        QTabBar#connectionSettingsTabs {{
            font-weight: 600;
        }}

        QTabBar#connectionSettingsTabs::tab {{
            min-width: 0;
            padding: 4px 12px;
        }}

        QToolButton#connectionType_ssh,
        QToolButton#connectionType_telnet,
        QToolButton#connectionType_rdp {{
            min-width: 58px;
            padding: 7px 9px;
            border: 1px solid transparent;
            border-radius: 4px;
            background-color: {colors.surface};
        }}

        QToolButton#connectionType_ssh:hover,
        QToolButton#connectionType_telnet:hover,
        QToolButton#connectionType_rdp:hover {{
            background-color: {colors.raised};
        }}

        QToolButton#connectionType_ssh:checked,
        QToolButton#connectionType_telnet:checked,
        QToolButton#connectionType_rdp:checked {{
            background-color: {colors.raised};
            border-color: {colors.accent};
        }}

        QToolButton#connectionType_ssh:focus:!checked,
        QToolButton#connectionType_telnet:focus:!checked,
        QToolButton#connectionType_rdp:focus:!checked {{
            border: 1px dotted {colors.accent};
        }}

        QTabBar#connectionSettingsTabs:focus {{
            border-bottom: 1px dotted {colors.muted_text};
        }}

        QLabel#connectionSectionSummary {{
            color: {colors.muted_text};
        }}

        QLabel#connectionSectionSummary[needsAttention="true"] {{
            color: {colors.danger};
        }}

        QPushButton#copyHostKeyFingerprintButton {{
            min-height: 28px;
            padding: 0 10px;
            background-color: {colors.raised};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 5px;
        }}

        QPushButton#copyHostKeyFingerprintButton:hover,
        QPushButton#copyHostKeyFingerprintButton:focus {{
            border-color: {colors.accent};
        }}

        QPushButton#keepSshHostKeyButton {{
            min-height: 40px;
            padding: 0 18px;
            background-color: {colors.accent};
            color: {colors.accent_text};
            border: 1px solid {colors.accent};
            border-radius: 7px;
            font-weight: 700;
        }}

        QPushButton#keepSshHostKeyButton:hover,
        QPushButton#keepSshHostKeyButton:focus {{
            border: 2px solid {colors.text};
        }}

        QPushButton#replaceSshHostKeyButton {{
            min-height: 40px;
            padding: 0 18px;
            background-color: {colors.warning_surface};
            color: {colors.warning_text};
            border: 1px solid {colors.warning};
            border-radius: 7px;
            font-weight: 600;
        }}

        QPushButton#replaceSshHostKeyButton:hover,
        QPushButton#replaceSshHostKeyButton:focus {{
            border: 2px solid {colors.warning};
        }}

        QPushButton#replaceSshHostKeyButton:pressed {{
            background-color: {colors.raised};
            color: {colors.text};
        }}

        QDialogButtonBox {{
            dialogbuttonbox-buttons-have-icons: 0;
        }}

        QPushButton[actionRole="primary"],
        QPushButton#saveConnectionButton,
        QPushButton#saveConnectionFolderButton,
        QPushButton#saveSettingsButton,
        QPushButton#confirmPasteButton,
        QPushButton#connectSshLoginButton,
        QPushButton#connectTelnetButton {{
            min-height: 36px;
            padding: 0 14px;
            background-color: {colors.accent};
            color: {colors.accent_text};
            border: 1px solid {colors.accent};
            border-radius: 7px;
            font-weight: 700;
        }}

        QPushButton[actionRole="primary"]:hover,
        QPushButton#saveConnectionButton:hover,
        QPushButton#saveConnectionFolderButton:hover,
        QPushButton#saveSettingsButton:hover,
        QPushButton#confirmPasteButton:hover,
        QPushButton#connectTelnetButton:hover,
        QPushButton#connectSshLoginButton:hover,
        QPushButton[actionRole="primary"]:focus,
        QPushButton#saveConnectionButton:focus,
        QPushButton#saveConnectionFolderButton:focus,
        QPushButton#saveSettingsButton:focus,
        QPushButton#confirmPasteButton:focus,
        QPushButton#connectTelnetButton:focus,
        QPushButton#connectSshLoginButton:focus {{
            border-color: {colors.text};
        }}

        QPushButton[actionRole="primary"]:pressed,
        QPushButton#saveConnectionButton:pressed,
        QPushButton#saveConnectionFolderButton:pressed,
        QPushButton#saveSettingsButton:pressed,
        QPushButton#confirmPasteButton:pressed,
        QPushButton#connectTelnetButton:pressed,
        QPushButton#connectSshLoginButton:pressed {{
            background-color: {colors.raised};
            color: {colors.text};
        }}

        QPushButton#saveConnectionButton:disabled,
        QPushButton#saveConnectionFolderButton:disabled,
        QPushButton#connectTelnetButton:disabled,
        QPushButton#connectSshLoginButton:disabled {{
            background-color: {colors.raised};
            color: {colors.muted_text};
            border-color: {colors.border};
        }}

        QPushButton#newConnectionButton,
        QPushButton#newFolderButton,
        QPushButton[actionRole="secondary"],
        QPushButton#cancelConnectionButton,
        QPushButton#cancelConnectionFolderButton,
        QPushButton#cancelSettingsButton,
        QPushButton#cancelDeleteButton,
        QPushButton#cancelPasteButton,
        QPushButton#cancelSshLoginButton,
        QPushButton#cancelCloseTerminalButton,
        QPushButton#closeSavedSshLoginsButton,
        QPushButton#cancelRemoveSavedSshLoginButton,
        QPushButton#cancelRemoveAllSavedSshLoginsButton,
        QPushButton#cancelRemoveChooserSshLoginButton,
        QPushButton#cancelTelnetButton {{
            min-height: 36px;
            padding: 0 14px;
            background-color: {colors.raised};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
        }}

        QPushButton#newConnectionButton:hover,
        QPushButton#newFolderButton:hover,
        QPushButton[actionRole="secondary"]:hover,
        QPushButton#cancelConnectionButton:hover,
        QPushButton#cancelConnectionFolderButton:hover,
        QPushButton#cancelSettingsButton:hover,
        QPushButton#cancelDeleteButton:hover,
        QPushButton#cancelPasteButton:hover,
        QPushButton#cancelTelnetButton:hover,
        QPushButton#cancelSshLoginButton:hover,
        QPushButton#cancelCloseTerminalButton:hover,
        QPushButton#closeSavedSshLoginsButton:hover,
        QPushButton#cancelRemoveSavedSshLoginButton:hover,
        QPushButton#cancelRemoveAllSavedSshLoginsButton:hover,
        QPushButton#newConnectionButton:focus,
        QPushButton#newFolderButton:focus,
        QPushButton[actionRole="secondary"]:focus,
        QPushButton#cancelConnectionButton:focus,
        QPushButton#cancelConnectionFolderButton:focus,
        QPushButton#cancelSettingsButton:focus,
        QPushButton#cancelDeleteButton:focus,
        QPushButton#cancelPasteButton:focus,
        QPushButton#cancelTelnetButton:focus,
        QPushButton#cancelSshLoginButton:focus,
        QPushButton#cancelCloseTerminalButton:focus,
        QPushButton#closeSavedSshLoginsButton:focus,
        QPushButton#cancelRemoveSavedSshLoginButton:focus,
        QPushButton#cancelRemoveAllSavedSshLoginsButton:focus {{
            border-color: {colors.text};
        }}

        QPushButton[actionRole="danger"],
        QPushButton#deleteConnectionButton,
        QPushButton#deleteConnectionFolderButton,
        QPushButton#confirmCloseTerminalButton,
        QPushButton#removeSavedSshLoginButton,
        QPushButton#removeAllSavedSshLoginsButton,
        QPushButton#confirmRemoveSavedSshLoginButton,
        QPushButton#confirmRemoveAllSavedSshLoginsButton {{
            min-height: 36px;
            padding: 0 14px;
            background-color: {colors.danger};
            color: {colors.danger_text};
            border: 1px solid {colors.danger};
            border-radius: 7px;
            font-weight: 700;
        }}

        QPushButton[actionRole="danger"]:hover,
        QPushButton#deleteConnectionButton:hover,
        QPushButton#deleteConnectionFolderButton:hover,
        QPushButton#confirmCloseTerminalButton:hover,
        QPushButton#removeSavedSshLoginButton:hover,
        QPushButton#removeAllSavedSshLoginsButton:hover,
        QPushButton#confirmRemoveSavedSshLoginButton:hover,
        QPushButton#confirmRemoveAllSavedSshLoginsButton:hover,
        QPushButton[actionRole="danger"]:focus,
        QPushButton#deleteConnectionButton:focus,
        QPushButton#deleteConnectionFolderButton:focus,
        QPushButton#confirmCloseTerminalButton:focus,
        QPushButton#removeSavedSshLoginButton:focus,
        QPushButton#removeAllSavedSshLoginsButton:focus,
        QPushButton#confirmRemoveSavedSshLoginButton:focus,
        QPushButton#confirmRemoveAllSavedSshLoginsButton:focus {{
            border-color: {colors.text};
        }}

        QPushButton[actionRole="primary"]:disabled,
        QPushButton[actionRole="secondary"]:disabled,
        QPushButton[actionRole="danger"]:disabled,
        QPushButton#removeSavedSshLoginButton:disabled,
        QPushButton#removeAllSavedSshLoginsButton:disabled {{
            background-color: {colors.raised};
            color: {colors.muted_text};
            border-color: {colors.border};
        }}

        QLineEdit,
        QPlainTextEdit,
        QTextEdit,
        QComboBox,
        QSpinBox {{
            min-height: 34px;
            padding: 0 {FORM_FIELD_HORIZONTAL_PADDING}px;
            background-color: {colors.surface};
            color: {colors.text};
            border: 1px solid {colors.control_border};
            border-radius: 7px;
        }}

        QLineEdit,
        QPlainTextEdit,
        QTextEdit,
        QComboBox {{
            placeholder-text-color: {colors.muted_text};
        }}

        QLineEdit:focus,
        QComboBox:focus,
        QSpinBox:focus {{
            border: 2px solid {colors.accent};
        }}

        QLineEdit#sshHostFingerprintLabel:focus {{
            border: 2px solid {colors.accent};
        }}

        QComboBox::drop-down {{
            border: none;
            width: 24px;
        }}

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

        QProgressBar#sshLoginResolutionProgress,
        QProgressBar#connectionTransferProgress {{
            min-height: 4px;
            max-height: 4px;
            background-color: {colors.raised};
            border: none;
            border-radius: 2px;
        }}

        QProgressBar#sshLoginResolutionProgress::chunk,
        QProgressBar#connectionTransferProgress::chunk {{
            background-color: {colors.accent};
            border-radius: 2px;
        }}
    """.strip()
