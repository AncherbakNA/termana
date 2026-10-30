"""Declarative menu composition; actions and session policy stay with their owners."""

import sys

from PySide6.QtWidgets import QMainWindow

from termana.i18n import tr

# None denotes a separator. These are existing action attributes, not new handlers.
WINDOW_MENUS = (
    (
        "Terminal",
        (
            "local_terminal_action",
            "saved_commands_action",
            "session_logging_action",
            "open_session_logs_action",
            "open_ssh_config_action",
            "import_gateway_settings_action",
            "group_input_action",
            "find_terminal_action",
            "rename_terminal_tab_action",
            "switch_terminal_action",
            "find_open_terminals_action",
            "ssh_session_details_action",
            "remote_files_action",
            "recover_local_drafts_action",
            "save_ssh_session_action",
            None,
            "close_current_terminal_action",
            "next_terminal_tab_action",
            "previous_terminal_tab_action",
            None,
            "split_right_action",
            "split_down_action",
            "move_current_to_next_pane_action",
            "merge_current_pane_action",
            "merge_all_panes_action",
            "focus_next_pane_action",
            "maximize_pane_action",
            None,
            "exit_action",
        ),
    ),
    (
        "Sessions",
        (
            "new_connection_action",
            "rdp_connection_action",
            "new_folder_action",
            None,
            "import_connections_action",
            "export_connections_action",
            "github_sync_action",
        ),
    ),
    (
        "View",
        (
            "toggle_sidebar_action",
            "toggle_toolbar_action",
            "fullscreen_action",
            None,
            "increase_terminal_font_action",
            "decrease_terminal_font_action",
            "reset_terminal_font_action",
        ),
    ),
    (
        "Settings",
        (
            "settings_action",
            None,
            "saved_ssh_logins_action",
            "ssh_accounts_action",
            "reset_telnet_warnings_action",
            None,
            "backup_configuration_action",
            "restore_configuration_action",
        ),
    ),
    ("Help", ("check_updates_action", "about_action")),
)


def create_window_menus(window: QMainWindow) -> None:
    """Reuse exact QAction identities, order and platform-native menu policy."""
    menu_bar = window.menuBar()
    menu_bar.setObjectName("mainMenuBar")
    menu_bar.setNativeMenuBar(sys.platform == "darwin")
    for title, attributes in WINDOW_MENUS:
        menu = menu_bar.addMenu(tr(title))
        for attribute in attributes:
            if attribute is None:
                menu.addSeparator()
            else:
                menu.addAction(getattr(window, attribute))
