"""Original stylesheet fragments grouped by UI responsibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.ui.theme import ThemeColors


def dialog_surfaces_styles(colors: ThemeColors) -> str:
    """Original dialog surfaces rules; whitespace and cascade are deliberate."""
    return f"""
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

        QScrollArea#githubSyncBodyScroll,
        QWidget#githubSyncBodyViewport,
        QWidget#githubSyncBody,
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
"""


def tables_styles(colors: ThemeColors) -> str:
    """Original tables rules; whitespace and cascade are deliberate."""
    return f"""
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
"""


def scrollbars_styles(
    colors: ThemeColors, *, scrollbar_extent: int, scrollbar_minimum_handle: int
) -> str:
    """Original scrollbars rules; whitespace and cascade are deliberate."""
    return f"""
        QScrollBar:vertical {{
            width: {scrollbar_extent}px;
            margin: 0;
            background-color: {colors.background};
        }}

        QScrollBar:horizontal {{
            height: {scrollbar_extent}px;
            margin: 0;
            background-color: {colors.background};
        }}

        QScrollBar::handle:vertical {{
            min-height: {scrollbar_minimum_handle}px;
            background-color: {colors.control_border};
            border-radius: 6px;
        }}

        QScrollBar::handle:horizontal {{
            min-width: {scrollbar_minimum_handle}px;
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
"""


def primary_buttons_styles(colors: ThemeColors) -> str:
    """Original primary buttons rules; whitespace and cascade are deliberate."""
    return f"""
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
"""


def secondary_buttons_styles(colors: ThemeColors) -> str:
    """Original secondary buttons rules; whitespace and cascade are deliberate."""
    return f"""
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
"""


def danger_buttons_styles(colors: ThemeColors) -> str:
    """Original danger buttons rules; whitespace and cascade are deliberate."""
    return f"""
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
"""


def fields_styles(colors: ThemeColors, *, form_field_horizontal_padding: int) -> str:
    """Original fields rules; whitespace and cascade are deliberate."""
    return f"""
        QLineEdit,
        QPlainTextEdit,
        QTextEdit,
        QComboBox,
        QSpinBox {{
            min-height: 34px;
            padding: 0 {form_field_horizontal_padding}px;
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
"""


def progress_styles(colors: ThemeColors) -> str:
    """Original progress rules; whitespace and cascade are deliberate."""
    return f"""
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
    """
