"""Cross-platform locations used by Termana."""

from pathlib import Path
from typing import Final

from PySide6.QtCore import QStandardPaths

CONNECTIONS_FILENAME: Final = "connections.json"
SETTINGS_FILENAME: Final = "settings.json"
COMMAND_SNIPPETS_FILENAME: Final = "saved-commands.json"
CREDENTIAL_INDEX_FILENAME: Final = "ssh-credential-index.json"
APPLICATION_LOCK_FILENAME: Final = "termana.lock"


def application_config_directory() -> Path:
    """Keep app-owned mutable data together, independent of the installed binary."""
    return Path.home() / ".termana"


def legacy_application_config_directory() -> Path:
    """Read the earlier OS location only for guarded, copy-only migration."""
    config_directory = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    if not config_directory:
        raise RuntimeError("The operating system did not provide a configuration path")
    return Path(config_directory)


def known_hosts_file_path() -> Path:
    """Termana trust is separate from other clients' ~/.ssh files."""
    return application_config_directory() / "known_hosts"


def session_logs_directory() -> Path:
    return application_config_directory() / "logs"


def connections_file_path() -> Path:
    """Return the platform-appropriate file for saved connections."""
    return application_config_directory() / CONNECTIONS_FILENAME


def settings_file_path() -> Path:
    """Return the platform-appropriate file for application preferences."""
    return application_config_directory() / SETTINGS_FILENAME


def command_snippets_file_path() -> Path:
    """Return the ordinary-text command library, separate from SSH credentials."""
    return application_config_directory() / COMMAND_SNIPPETS_FILENAME


def credential_index_file_path() -> Path:
    """Return the non-secret metadata index for system-keychain SSH passwords."""
    return application_config_directory() / CREDENTIAL_INDEX_FILENAME


def application_lock_file_path() -> Path:
    """Return the lock that serializes access to application configuration."""
    return application_config_directory() / APPLICATION_LOCK_FILENAME
