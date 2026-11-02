"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDialog, QMenu, QToolButton

from termana import APP_NAME
from termana.app.window_contracts import TOOLBAR_ICON_EXTENT
from termana.app.window_fullscreen_controller import WindowFullscreenController
from termana.app.window_menus import create_window_menus
from termana.models.settings import ThemeId
from termana.models.terminal_layout import terminal_grid_layout
from termana.terminal.dialogs.terminal_grid_dialog import TerminalGridDialog
from termana.terminal.tab_shortcuts import TerminalTabAction, shortcut_for_terminal_tab_action
from termana.terminal.zoom import ZoomAction, shortcuts_for_zoom_action
from termana.ui.i18n import tr
from termana.ui.theme import ThemeColors
from termana.ui.ui_actions import create_window_action
from termana.ui.ui_icons import interface_icon
from termana.ui.ui_toolbar import WORKBENCH_TOOLS, WorkbenchToolBar, workbench_icon_colors


def _create_actions(self: MainWindow) -> None:
    """Create commands that can be shared by menus and toolbars."""
    _create_connection_actions(self)
    _create_terminal_navigation_actions(self)
    _create_pane_actions(self)
    _create_session_tools_actions(self)
    _create_application_actions(self)
    _create_window_actions(self)


def _create_connection_actions(self: MainWindow) -> None:
    from termana.rdp.ui import open_rdp_dialog

    self.rdp_connection_action = create_window_action(
        self,
        tr("RDP connection…"),
        "rdpConnectionAction",
        lambda _checked=False: open_rdp_dialog(self),
        status_tip=tr("Open an embedded remote Windows desktop"),
    )
    self.new_connection_action = create_window_action(
        self,
        tr("New connection"),
        "newConnectionAction",
        self._open_new_connection_dialog,
        status_tip=tr("Create a saved remote connection"),
        tool_tip=tr("Create a saved remote connection"),
    )
    self.new_folder_action = create_window_action(
        self,
        tr("New folder"),
        "newConnectionFolderAction",
        self._open_new_connection_folder,
        status_tip=tr("Create a folder for saved connections"),
    )
    self.reset_telnet_warnings_action = create_window_action(
        self,
        tr("Show Telnet warnings again"),
        "resetTelnetWarningsAction",
        self._reset_telnet_warnings,
        status_tip=tr("Restore the encryption warning for all Telnet destinations"),
    )
    self.import_connections_action = create_window_action(
        self,
        tr("Import connections…"),
        "importConnectionsAction",
        self._open_connection_import_dialog,
        status_tip=tr("Add SSH and Telnet connections from a Termana JSON file"),
    )
    self.export_connections_action = create_window_action(
        self,
        tr("Export all connections…"),
        "exportConnectionsAction",
        self._open_connection_export_dialog,
        status_tip=tr("Save all SSH and Telnet connections without passwords or saved logins"),
    )


def _create_terminal_navigation_actions(self: MainWindow) -> None:
    self.local_terminal_action = create_window_action(
        self,
        tr("Local terminal"),
        "localTerminalAction",
        self._open_local_terminal,
        status_tip=tr("Open a terminal on this computer"),
        tool_tip=tr("Open a terminal on this computer"),
        shortcuts=(shortcut_for_terminal_tab_action(TerminalTabAction.NEW_LOCAL),),
        auto_repeat=False,
    )
    self.recover_local_drafts_action = create_window_action(
        self,
        tr("Recover local drafts…"),
        "recoverLocalDraftsAction",
        self._open_private_recovery_dialog,
        status_tip=tr("Review inactive private local copies without reconnecting or uploading"),
    )
    self.close_current_terminal_action = create_window_action(
        self,
        tr("Close current terminal"),
        "closeCurrentTerminalAction",
        self.terminal_workspace.request_close_current_terminal,
        status_tip=tr("Close the current terminal tab"),
        shortcuts=(shortcut_for_terminal_tab_action(TerminalTabAction.CLOSE_CURRENT),),
        auto_repeat=False,
    )
    self.next_terminal_tab_action = create_window_action(
        self,
        tr("Next terminal tab"),
        "nextTerminalTabAction",
        self.terminal_workspace.activate_next_terminal,
        status_tip=tr("Select the next terminal tab"),
        shortcuts=(shortcut_for_terminal_tab_action(TerminalTabAction.NEXT),),
        auto_repeat=False,
    )
    self.previous_terminal_tab_action = create_window_action(
        self,
        tr("Previous terminal tab"),
        "previousTerminalTabAction",
        self.terminal_workspace.activate_previous_terminal,
        status_tip=tr("Select the previous terminal tab"),
        shortcuts=(shortcut_for_terminal_tab_action(TerminalTabAction.PREVIOUS),),
        auto_repeat=False,
    )


def _create_pane_actions(self: MainWindow) -> None:
    pane_actions = (
        ("split_right_action", tr("Split right"), "splitRightAction", "split_right"),
        ("split_down_action", tr("Split down"), "splitDownAction", "split_down"),
        (
            "move_current_to_next_pane_action",
            tr("Move current tab to next pane"),
            "moveCurrentToNextPaneAction",
            "move_current_to_next_pane",
        ),
        (
            "merge_current_pane_action",
            tr("Merge current pane"),
            "mergeCurrentPaneAction",
            "merge_current_pane",
        ),
        ("merge_all_panes_action", tr("Merge all panes"), "mergeAllPanesAction", "merge_all_panes"),
        ("focus_next_pane_action", tr("Focus next pane"), "focusNextPaneAction", "focus_next_pane"),
    )
    for attribute, label, object_name, method_name in pane_actions:
        action = create_window_action(
            self,
            label,
            object_name,
            getattr(self.terminal_workspace, method_name),
            status_tip=label,
            auto_repeat=False,
        )
        setattr(self, attribute, action)
    self.merge_all_panes_action.setStatusTip(
        tr("Return to one pane, keeping all tabs and connections open")
    )
    self.pane_layout_menu = QMenu(tr("Split"), self)
    self.pane_layout_action = self.pane_layout_menu.menuAction()
    self.pane_layout_action.setObjectName("paneLayoutAction")
    self.pane_layout_action.setStatusTip(
        tr("Choose a layout, keeping all tabs and connections open")
    )
    for attribute, label, columns, rows in (
        ("pane_columns_action", tr("Two panes side by side (2 columns, 1 row)"), 2, 1),
        ("pane_rows_action", tr("Two panes stacked (1 column, 2 rows)"), 1, 2),
        ("pane_one_action", tr("One pane (Unsplit)"), 1, 1),
    ):
        action = create_window_action(
            self,
            label,
            attribute,
            lambda _checked=False, c=columns, r=rows: self.terminal_workspace.apply_pane_layout(
                terminal_grid_layout(c, r)
            ),
            auto_repeat=False,
        )
        setattr(self, attribute, action)
        self.pane_layout_menu.addAction(action)
    self.pane_layout_menu.addSeparator()
    self.pane_custom_action = create_window_action(
        self, tr("Custom grid…"), "paneCustomAction", self._choose_terminal_grid, auto_repeat=False
    )
    self.pane_layout_menu.addAction(self.pane_custom_action)


def _create_session_tools_actions(self: MainWindow) -> None:
    self.maximize_pane_action = create_window_action(
        self,
        tr("Maximize active pane"),
        "maximizePaneAction",
        self.terminal_workspace.toggle_maximize_pane,
        auto_repeat=False,
        checkable=True,
    )
    self.rename_terminal_tab_action = create_window_action(
        self,
        tr("Rename current tab…"),
        "renameTerminalTabAction",
        self.terminal_workspace.rename_current_terminal,
        status_tip=tr("Give this terminal tab a custom name"),
    )
    self.switch_terminal_action = create_window_action(
        self,
        tr("Switch terminal…"),
        "switchTerminalAction",
        self._open_terminals_controller.open_dialog,
        status_tip=tr("Find and select an open terminal in any pane"),
        auto_repeat=False,
    )
    self.find_open_terminals_action = create_window_action(
        self,
        tr("Find in open terminals…"),
        "findOpenTerminalsAction",
        self._terminal_multi_search_controller.open_dialog,
        status_tip=tr("Search explicit snapshots of selected terminal output"),
        auto_repeat=False,
    )
    self.ssh_session_details_action = create_window_action(
        self,
        tr("SSH connection details…"),
        "sshSessionDetailsAction",
        self._ssh_session_details_controller.open_dialog,
        status_tip=tr("View SSH connection settings, status and diagnostic details"),
    )
    self.remote_files_action = create_window_action(
        self,
        tr("Remote files"),
        "remoteFilesAction",
        self._sftp_controller.open_files,
        status_tip=tr("Browse and transfer files through an authenticated embedded SSH session"),
    )
    self.save_ssh_session_action = create_window_action(
        self,
        tr("Save SSH session as profile…"),
        "saveSshSessionAction",
        self._ssh_session_save_controller.open_dialog,
        status_tip=tr("Save a new profile without destination credentials"),
    )
    self.saved_commands_action = create_window_action(
        self,
        tr("Saved commands…"),
        "savedCommandsAction",
        lambda _checked=False: self._show_commands_sidebar(),
        status_tip=tr("Manage commands and insert without sending Enter"),
    )
    self.find_terminal_action = create_window_action(
        self,
        tr("Find in active terminal"),
        "findTerminalAction",
        self.terminal_workspace.open_current_terminal_search,
        status_tip=tr("Search the output of the active terminal"),
    )


def _create_application_actions(self: MainWindow) -> None:
    self.settings_action = create_window_action(
        self,
        tr("Settings"),
        "settingsAction",
        self._open_settings_dialog,
        shortcuts=(QKeySequence(QKeySequence.StandardKey.Preferences),),
        tool_tip=tr("Open application settings"),
    )
    self.saved_ssh_logins_action = create_window_action(
        self,
        tr("Saved SSH logins…"),
        "savedSshLoginsAction",
        self._open_saved_ssh_logins_dialog,
        status_tip=tr("View and remove SSH passwords saved by Termana"),
    )
    self.saved_ssh_logins_action.setEnabled(self._credential_repository is not None)
    self.ssh_accounts_action = create_window_action(
        self,
        "SSH accounts…",
        "sshAccountsAction",
        self._manage_ssh_accounts,
        status_tip="Create reusable SSH accounts and link them to connection profiles",
    )
    self.ssh_accounts_action.setEnabled(self._credential_repository is not None)
    from termana.ui.configuration_backup_ui import ConfigurationBackupUi

    self._configuration_backup_ui = ConfigurationBackupUi(self)
    self.backup_configuration_action = create_window_action(
        self,
        "Back up configuration…",
        "backupConfigurationAction",
        self._configuration_backup_ui.back_up,
    )
    self.restore_configuration_action = create_window_action(
        self,
        "Restore configuration…",
        "restoreConfigurationAction",
        self._configuration_backup_ui.restore,
    )
    self.about_action = create_window_action(
        self, tr("About {p0}", p0=f"{APP_NAME}"), "aboutAction", self._show_about_dialog
    )
    self.check_updates_action = create_window_action(
        self,
        "Check for updates…",
        "checkUpdatesAction",
        self._show_update_dialog,
        status_tip="Check signed releases without closing sessions",
    )
    self.github_sync_action = create_window_action(
        self,
        "GitHub session sync…",
        "githubSyncAction",
        self._show_sync_dialog,
        status_tip="Synchronize encrypted saved connections with a private repository",
    )


def _create_window_actions(self: MainWindow) -> None:
    self.toggle_sidebar_action = QAction(tr("Sidebar"), self)
    self.toggle_sidebar_action.setObjectName("toggleConnectionsSidebarAction")
    self.toggle_sidebar_action.setCheckable(True)
    self.toggle_sidebar_action.setChecked(True)
    self.toggle_sidebar_action.toggled.connect(self._set_sidebar_visible)
    self.increase_terminal_font_action = create_window_action(
        self,
        tr("Increase terminal font size"),
        "increaseTerminalFontAction",
        self._terminal_zoom_controller.increase,
        status_tip=tr("Increase the font size in every terminal"),
        shortcuts=shortcuts_for_zoom_action(ZoomAction.INCREASE),
        auto_repeat=False,
    )
    self.decrease_terminal_font_action = create_window_action(
        self,
        tr("Decrease terminal font size"),
        "decreaseTerminalFontAction",
        self._terminal_zoom_controller.decrease,
        status_tip=tr("Decrease the font size in every terminal"),
        shortcuts=shortcuts_for_zoom_action(ZoomAction.DECREASE),
        auto_repeat=False,
    )
    self.reset_terminal_font_action = create_window_action(
        self,
        tr("Reset terminal font size"),
        "resetTerminalFontAction",
        self._terminal_zoom_controller.reset,
        status_tip=tr("Restore the default terminal font size"),
        shortcuts=shortcuts_for_zoom_action(ZoomAction.RESET),
        auto_repeat=False,
    )
    self.exit_action = create_window_action(
        self,
        tr("Exit"),
        "exitAction",
        self.close,
        shortcuts=(QKeySequence(QKeySequence.StandardKey.Quit),),
    )
    self._fullscreen_controller = WindowFullscreenController(self)
    self.fullscreen_action = self._fullscreen_controller.action


def _create_menu_bar(self: MainWindow) -> None:
    """Create the application menu hierarchy."""
    create_window_menus(self)


def _choose_terminal_grid(self: MainWindow, _checked: bool = False) -> None:
    dialog = TerminalGridDialog(self)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        self.terminal_workspace.apply_pane_layout(dialog.selected_layout())
    dialog.deleteLater()


def _create_toolbar(self: MainWindow) -> None:
    """Present real shared commands as labeled workbench tools, never placeholders."""
    toolbar = WorkbenchToolBar(tr("Main toolbar"))
    toolbar.setObjectName("mainToolBar")
    toolbar.setMovable(False)
    toolbar.setFloatable(False)
    toolbar.setIconSize(QSize(TOOLBAR_ICON_EXTENT, TOOLBAR_ICON_EXTENT))
    toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
    self.main_toolbar = toolbar
    self._apply_toolbar_actions(self._settings_controller.settings.toolbar_actions)
    self._refresh_action_icons(self._settings_controller.colors)
    self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
    self.toggle_toolbar_action = toolbar.toggleViewAction()
    self.toggle_toolbar_action.setObjectName("toggleMainToolbarAction")
    self.toggle_toolbar_action.setText(tr("Main toolbar"))


def _apply_toolbar_actions(self: MainWindow, selected: tuple[str, ...]) -> None:
    """Remove only toolbar presentations, never hide the shared menu QAction."""
    toolbar = self.main_toolbar
    for action in toolbar.actions():
        toolbar.removeAction(action)
    button_names = {
        "new_connection_action": "toolbarNewConnectionButton",
        "local_terminal_action": "toolbarLocalTerminalButton",
        "settings_action": "toolbarSettingsButton",
    }
    for tool in WORKBENCH_TOOLS:
        if tool.action_attribute not in selected:
            continue
        action = getattr(self, tool.action_attribute)
        action.setIconText(tr(tool.label, context="toolbar"))
        if action.toolTip() == action.text():
            action.setToolTip(action.statusTip() or action.text())
        toolbar.addAction(action)
        button = toolbar.widgetForAction(action)
        if action is self.pane_layout_action and isinstance(button, QToolButton):
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        if button is not None:
            button.setObjectName(
                button_names.get(tool.action_attribute, f"toolbar_{action.objectName()}")
            )
            button.setAccessibleName(action.text())


def _refresh_action_icons(self: MainWindow, colors: ThemeColors) -> None:
    """Keep toolbar and menu glyphs legible when the live theme changes."""
    tones = workbench_icon_colors(
        colors,
        monochrome=self._settings_controller.displayed_theme
        in (ThemeId.MONOCHROME, ThemeId.MONOCHROME_LIGHT),
    )
    for tool in WORKBENCH_TOOLS:
        action = getattr(self, tool.action_attribute)
        action.setIconVisibleInMenu(False)
        action.setIcon(interface_icon(tool.icon, tones[tool.tone], size=TOOLBAR_ICON_EXTENT))
    for action, icon in (
        (self.pane_columns_action, "split"),
        (self.pane_rows_action, "split_rows"),
        (self.pane_one_action, "single_pane"),
        (self.pane_custom_action, "pane_grid"),
    ):
        action.setIconVisibleInMenu(True)
        action.setIcon(interface_icon(icon, tones["layout"]))
