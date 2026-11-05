"""Original stylesheet fragments grouped by UI responsibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.ui.theme import ThemeColors


def messages_styles(colors: ThemeColors) -> str:
    """Original messages rules; whitespace and cascade are deliberate."""
    return f"""
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
"""


def saved_logins_styles(colors: ThemeColors) -> str:
    """Original saved logins rules; whitespace and cascade are deliberate."""
    return f"""
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
"""


def host_keys_styles(colors: ThemeColors) -> str:
    """Original host keys rules; whitespace and cascade are deliberate."""
    return f"""
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
"""


def ssh_controls_styles(colors: ThemeColors) -> str:
    """Original ssh controls rules; whitespace and cascade are deliberate."""
    return f"""
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
        QToolButton#connectionType_rdp,
        QToolButton#connectionType_sftp,
        QToolButton#connectionType_ftp,
        QToolButton#connectionType_tftp,
        QToolButton#connectionType_database {{
            min-width: 58px;
            padding: 7px 9px;
            border: 1px solid transparent;
            border-radius: 4px;
            background-color: {colors.surface};
        }}

        QToolButton#connectionType_ssh:hover,
        QToolButton#connectionType_telnet:hover,
        QToolButton#connectionType_rdp:hover,
        QToolButton#connectionType_sftp:hover,
        QToolButton#connectionType_ftp:hover,
        QToolButton#connectionType_tftp:hover,
        QToolButton#connectionType_database:hover {{
            background-color: {colors.raised};
        }}

        QToolButton#connectionType_ssh:checked,
        QToolButton#connectionType_telnet:checked,
        QToolButton#connectionType_rdp:checked,
        QToolButton#connectionType_sftp:checked,
        QToolButton#connectionType_ftp:checked,
        QToolButton#connectionType_tftp:checked,
        QToolButton#connectionType_database:checked {{
            background-color: {colors.raised};
            border-color: {colors.accent};
        }}

        QToolButton#connectionType_ssh:focus:!checked,
        QToolButton#connectionType_telnet:focus:!checked,
        QToolButton#connectionType_rdp:focus:!checked,
        QToolButton#connectionType_sftp:focus:!checked,
        QToolButton#connectionType_ftp:focus:!checked,
        QToolButton#connectionType_tftp:focus:!checked,
        QToolButton#connectionType_database:focus:!checked {{
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
"""
