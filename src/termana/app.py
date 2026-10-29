"""Application entry point."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from termana.i18n import tr


def main(arguments: Sequence[str] | None = None) -> int:
    """Start the desktop application from the active Python environment."""
    selected_arguments = list(sys.argv[1:] if arguments is None else arguments)
    from termana.credentials.askpass_helper import is_askpass_invocation
    from termana.credentials.askpass_helper import main as askpass_main

    if is_askpass_invocation():
        return askpass_main([sys.argv[0], *selected_arguments])
    parser = argparse.ArgumentParser(
        prog="python -m termana", description="Termana SSH, Telnet and local terminals; optional embedded RDP component."
    )
    from termana.build_info import version_label

    parser.add_argument("--version", action="version", version=version_label())
    parser.add_argument(
        "--ssh-engine",
        choices=("system", "embedded"),
        default=None,
        help="Override the saved SSH engine for this run. Without a flag, use Preferences (default embedded). System is explicit compatibility mode; no automatic fallback.",
    )
    parser.add_argument(
        "--ssh-agent",
        type=Path,
        metavar="ABSOLUTE_SOCKET_PATH",
        help="Explicit opt-in to a Unix SSH Agent for embedded SSH. No inherited agent or forwarding.",
    )
    parser.add_argument(
        "--ssh-known-hosts",
        type=Path,
        metavar="ABSOLUTE_PATH",
        help="Explicit trust file for embedded SSH (default: ~/.termana/known_hosts). A missing file requires verified consent; verification is never disabled.",
    )
    parser.add_argument(
        "--check-runtime",
        metavar="REPORT.json",
        help="Check the GUI, Python terminal helper and system SSH without user data; write a new report.",
    )
    parser.add_argument(
        "--check-rdp",
        metavar="REPORT.json",
        help="Verify the bundled RDP component offline, without connecting or opening user data.",
    )
    parser.add_argument(
        "--check-transports",
        metavar="REPORT.json",
        help="Check Embedded SSH/SFTP and System SFTP against a disposable loopback peer only.",
    )
    options = parser.parse_args(selected_arguments)
    if options.check_rdp is not None:
        if any((options.check_runtime, options.check_transports, options.ssh_engine,
                options.ssh_agent, options.ssh_known_hosts)):
            parser.error("--check-rdp is a standalone isolated diagnostic")
        from termana.rdp.component_check import main as component_check_main
        return component_check_main(options.check_rdp)
    if options.check_transports is not None:
        if any(
            (options.check_runtime, options.ssh_engine, options.ssh_agent, options.ssh_known_hosts)
        ):
            parser.error("--check-transports is a standalone isolated diagnostic")
        from termana.runtime_transport_check import main as transport_check_main

        return transport_check_main(options.check_transports)
    if options.ssh_known_hosts is not None and options.ssh_engine != "embedded":
        parser.error("--ssh-known-hosts requires --ssh-engine embedded")
    if options.ssh_known_hosts is not None and not options.ssh_known_hosts.is_absolute():
        parser.error("--ssh-known-hosts must be an absolute path")
    if options.ssh_agent is not None:
        if options.ssh_engine != "embedded":
            parser.error("--ssh-agent requires --ssh-engine embedded")
        if sys.platform == "win32" or not options.ssh_agent.is_absolute():
            parser.error(
                "--ssh-agent requires an absolute Unix socket path; Windows embedded agents are unsupported"
            )
    if options.check_runtime is not None and options.ssh_engine not in (None, "system"):
        parser.error(
            "--check-runtime checks system OpenSSH compatibility; do not combine engine selection"
        )
    if options.check_runtime is not None:
        from termana.runtime_check import main as runtime_check_main

        return runtime_check_main(options.check_runtime)
    if options.ssh_engine == "embedded":
        try:
            from termana.credentials.embedded_ssh_auth import require_embedded_auth_contract

            require_embedded_auth_contract()
        except (ImportError, RuntimeError):
            print(
                "Embedded SSH is unavailable. Repair the normal Termana installation in a "
                "compatible Python environment, or explicitly launch with --ssh-engine system. "
                "No automatic fallback was performed.",
                file=sys.stderr,
            )
            return 1
        from termana.paths import known_hosts_file_path
        trust = options.ssh_known_hosts or known_hosts_file_path()
        return _run_gui(
            (), embedded_ssh_known_hosts=trust, embedded_ssh_agent_path=options.ssh_agent
        )
    if options.ssh_engine == "system":
        return _run_gui((), ssh_engine_override="system")
    return _run_gui(())


def _run_gui(
    arguments: Sequence[str],
    *,
    embedded_ssh_known_hosts: Path | None = None,
    embedded_ssh_agent_path: Path | None = None,
    ssh_engine_override: str | None = None,
) -> int:
    """Own the application lock while the Qt desktop event loop is running."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from termana import APP_NAME
    from termana.credentials.repository import SshCredentialRepository
    from termana.credentials.secret_store import KeyringSecretStore
    from termana.data_directory import DataDirectoryError, acquire_data_ownership
    from termana.i18n import application_language
    from termana.instance_lock import ApplicationLockError, acquire_application_lock
    from termana.main_window import MainWindow
    from termana.models.settings import DEFAULT_APPLICATION_SETTINGS, InterfaceLanguage, SshEngine
    from termana.paths import (
        application_config_directory,
        application_lock_file_path,
        command_snippets_file_path,
        connections_file_path,
        credential_index_file_path,
        known_hosts_file_path,
        legacy_application_config_directory,
        settings_file_path,
    )
    from termana.rdp.identity_repository import RdpIdentityRepository
    from termana.rdp.secret_store import RdpCredentialVault
    from termana.rdp.trust_store import RdpTrustStore
    from termana.settings_controller import SettingsController
    from termana.storage.command_snippet_store import CommandSnippetStore
    from termana.storage.configuration_backup import ConfigurationBackupError, apply_pending_restore
    from termana.storage.connection_store import ConnectionStore
    from termana.storage.credential_index_store import CredentialIndexStore
    from termana.storage.settings_store import SettingsStore, SettingsStoreError
    from termana.terminal.ssh_command import resolve_system_ssh_program
    from termana.terminal.system_ssh_config import SystemSshConfigStore
    from termana.theme import DEFAULT_THEME, build_stylesheet
    from termana.ui_icons import application_icon

    if ssh_engine_override not in (None, "system") or (
        ssh_engine_override is not None and embedded_ssh_known_hosts is not None
    ):
        raise ValueError("Unsupported startup engine override")
    application = QApplication([sys.argv[0], *arguments])
    application.setApplicationName(APP_NAME)
    application.setWindowIcon(application_icon())
    application.setStyleSheet(build_stylesheet(DEFAULT_THEME))

    try:
        data_root = settings_file_path().parent
        if data_root == application_config_directory():
            legacy = (
                legacy_application_config_directory()
                if data_root == Path.home() / ".termana" else None
            )
            application_lock = acquire_data_ownership(data_root, legacy=legacy)
        else:
            # Explicitly injected per-store locations (isolated startup tests).
            application_lock = acquire_application_lock(application_lock_file_path())
    except (ApplicationLockError, DataDirectoryError) as error:
        message_box = QMessageBox()
        message_box.setWindowTitle(tr("Termana could not start"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(str(error))
        message_box.setInformativeText(
            tr("The application stopped before opening or changing saved application data.")
        )
        message_box.exec()
        return 1

    try:
        while True:
            try:
                apply_pending_restore(settings_file_path().parent)
                break
            except (OSError, ConfigurationBackupError):
                from termana.dialogs.configuration_recovery_dialog import (
                    show_configuration_recovery,
                )

                if not show_configuration_recovery(settings_file_path().parent):
                    return 1
        connection_store = ConnectionStore(connections_file_path())
        credential_repository = SshCredentialRepository.from_stores(
            CredentialIndexStore(credential_index_file_path()),
            KeyringSecretStore(),
        )
        settings_store = SettingsStore(settings_file_path())
        settings_load_error: str | None = None
        try:
            application_settings = settings_store.load()
        except SettingsStoreError as error:
            application_settings = DEFAULT_APPLICATION_SETTINGS
            settings_load_error = str(error)

        # A saved choice is an explicit next-launch preference, never a live
        # reroute or a fallback. Keep the GUI available even if the primary
        # engine is missing: its local preflight refuses SSH before credentials
        # and Preferences can select system for the next launch.
        if (
            embedded_ssh_known_hosts is None
            and ssh_engine_override is None
            and application_settings.ssh_engine is SshEngine.EMBEDDED
        ):
            embedded_ssh_known_hosts = known_hosts_file_path()

        # Translation is temporarily deferred by product choice. Keep old
        # language preferences readable without changing the user's file.
        with application_language(application, InterfaceLanguage.ENGLISH):
            settings_controller = SettingsController(
                application,
                initial_settings=application_settings,
                store=settings_store,
                automatic_persistence_enabled=settings_load_error is None,
                parent=application,
            )
            window = MainWindow(
                connection_store=connection_store,
                command_snippet_store=CommandSnippetStore(command_snippets_file_path()),
                credential_repository=credential_repository,
                # A missing native client is an unavailable capability, not an
                # executable name to resolve through PATH or the working directory.
                # Explicit embedded SSH does not require a system client at all.
                ssh_program=(
                    resolve_system_ssh_program() if embedded_ssh_known_hosts is None else None
                ),
                settings_controller=settings_controller,
                settings_load_error=settings_load_error,
                embedded_ssh_known_hosts=embedded_ssh_known_hosts,
                embedded_ssh_agent_path=embedded_ssh_agent_path,
                system_ssh_config_store=SystemSshConfigStore(application_config_directory()),
                rdp_identity_repository=RdpIdentityRepository(RdpCredentialVault(),
                    RdpTrustStore(application_config_directory()/"rdp-certificates.json"), parent=application),
            )
            window.show()
            return application.exec()
    finally:
        application_lock.unlock()
