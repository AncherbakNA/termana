"""Coordinate sidebar browsers while keeping transfers on their exact SSH owner."""

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from shiboken6 import isValid

from termana.i18n import tr
from termana.paths import known_hosts_file_path
from termana.qt_lifecycle import schedule_delete_later
from termana.theme import DEFAULT_THEME
from termana.widgets.sftp_browser import SftpBrowser
from termana.widgets.terminal_page import TerminalPage


class SftpController(QObject):
    def __init__(self, window, workspace, sidebar, *, auto_open=False):
        super().__init__(window)
        if type(auto_open) is not bool:
            raise TypeError("auto_open must be a bool")
        self._window = window
        self._workspace = workspace
        self._sidebar = sidebar
        self._browsers = {}
        self._watched_pages = {}
        self._closing = False
        self._file_mode_requested = False
        self._auto_open = auto_open
        self._auto_open_seen = set()
        self._colors = DEFAULT_THEME
        self._monochrome = False
        self._system_sessions = {}
        self._file_secrets = {}
        sidebar.files_requested.connect(self.open_files)
        sidebar.connections_button.clicked.connect(self.show_connections)
        sidebar.commands_requested.connect(self.show_commands)
        sidebar.transfers.cancel_requested.connect(self._cancel_transfer)
        workspace.active_terminal_changed.connect(self._sync_active)
        workspace.open_sessions_changed.connect(self._sync_active)
        workspace.ssh_secret_submitted.connect(self._submit_file_secret)
        workspace.ssh_secret_cancel_requested.connect(self._cancel_file_secret)

    def _active_page(self):
        page = self._workspace.tabs.currentWidget()
        return page if isinstance(page, TerminalPage) else None

    def _provider(self, page):
        sessions = self._system_sessions.get(page.backend, ()) if page is not None else ()
        return sessions[-1] if sessions else page.backend if page is not None else None

    def _system_profile(self, page):
        if (
            page is None
            or page.session_ended
            or hasattr(page.backend, "sftp_available")
            or not page.backend.is_running
        ):
            return None
        return self._workspace.ssh_session_profile(page.session_id)

    def _available(self, page):
        return (
            page is not None
            and not page.session_ended
            and getattr(self._provider(page), "sftp_available", False)
        )

    def _sync_active(self, *_ignored):
        if self._closing:
            return
        page = self._active_page()
        watched = self._watched_pages.get(page)
        if watched is not None and watched[0] is not page.backend:
            page.destroyed.disconnect(watched[1])
            self._forget_page(page)
        if page is not None and page not in self._watched_pages:
            backend = page.backend

            def destroyed():
                # Parent destruction can delete this controller before a page;
                # the Python callback must not invoke a deleted QObject's slots.
                if isValid(self) and not self._closing:
                    self._forget_page(page)

            self._watched_pages[page] = (backend, destroyed)
            backend.started.connect(self._sync_active)
            backend.finished.connect(self._sync_active)
            page.destroyed.connect(destroyed)
        available = self._available(page)
        system_profile = self._system_profile(page)
        # Tab insertion precedes backend.start(). Keep the user's Files choice
        # through that short startup, but never carry it into local/Telnet.
        if page is None or (not hasattr(page.backend, "sftp_available") and system_profile is None):
            self._file_mode_requested = False
        explanation = None
        if system_profile is not None:
            explanation = tr(
                "Files opens a separate System OpenSSH connection for this tab and may ask you to sign in again."
            )
        elif available:
            explanation = tr(
                "Remote files on the current verified embedded SSH session. The server needs SFTP."
            )
        if not available:
            if (page is not None and getattr(getattr(page.backend, "plan", None), "profile", None) is not None
                    and page.backend.plan.profile.ssh_algorithms.protocol == "1"):
                explanation = tr("SFTP is not available in the legacy SSH1 component. Use a separate SSH2 profile for file transfers.")
            elif page is not None and hasattr(page.backend, "sftp_available"):
                explanation = (
                    tr("SSH ended. Reconnect this embedded SSH session to open Files again.")
                    if page.session_ended
                    else tr("Files becomes available after this embedded SSH session connects.")
                )
            elif system_profile is None:
                explanation = tr(
                    "Files requires a running SSH session with server-side SFTP. Local shells and Telnet do not provide SFTP."
                )
        self._sidebar.set_files_available(
            available or system_profile is not None, explanation=explanation
        )
        if not available and system_profile is not None and self._sidebar.showing_files:
            # A Files button may offer a new independent connection, but its
            # availability must never keep another tab's live browser visible.
            self._sidebar.show_connections()
        if available and page.backend not in self._auto_open_seen:
            self._auto_open_seen.add(page.backend)
            focus = QApplication.focusWidget()
            sidebar_has_focus = focus is self._sidebar or (
                focus is not None and self._sidebar.isAncestorOf(focus)
            )
            if (
                self._auto_open
                and self._window.toggle_sidebar_action.isChecked()
                and not sidebar_has_focus
            ):
                self._file_mode_requested = True
        if available and self._file_mode_requested:
            self._sidebar.show_files(self._browser_for(page))

    def show_connections(self, _checked=False):
        self._file_mode_requested = False
        page = self._active_page()
        if page is not None:
            self._auto_open_seen.add(page.backend)
        self._sidebar.show_connections()

    def show_commands(self, _checked=False):
        self._file_mode_requested = False
        page = self._active_page()
        if page is not None:
            self._auto_open_seen.add(page.backend)
        self._sidebar.show_commands()

    def set_auto_open(self, enabled):
        """Apply the file-panel preference only to future connection readiness."""
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        self._auto_open = enabled
        # Applying Preferences must not switch an already connected terminal.
        for page in self._watched_pages:
            if isValid(page) and self._available(page):
                self._auto_open_seen.add(page.backend)

    def open_files(self, _checked=False):
        if self._closing:
            return
        page = self._active_page()
        profile = self._system_profile(page)
        if not self._available(page) and profile is not None:
            provider = self._provider(page)
            if provider is not page.backend and not provider.is_finished:
                self._window.statusBar().showMessage(
                    tr("The separate SFTP connection is still signing in."), 6000
                )
                return
            self._open_system_files(page, profile)
            return
        if not self._available(page):
            QMessageBox.information(
                self._window,
                tr("Remote files"),
                tr(
                    "Open a running SSH session to a trusted server with SFTP enabled.\n\nEmbedded SSH uses its existing verified connection. System OpenSSH asks for permission to open a separate file connection for that tab and may ask you to sign in again. Local shells and Telnet do not provide SFTP."
                ),
            )
            return
        self._window.toggle_sidebar_action.setChecked(True)
        self._file_mode_requested = True
        self._auto_open_seen.add(page.backend)
        self._sidebar.show_files(self._browser_for(page))

    def _browser_for(self, page):
        backend = self._provider(page)
        if backend not in self._browsers:
            # Owner identity does not change when the terminal is renamed.
            plan = getattr(backend, "plan", None)
            owner = page.display_title
            if plan is not None:
                owner = f"{plan.profile.username}@{plan.endpoint.host}:{plan.endpoint.port} · {page.display_title}"
            browser = SftpBrowser(self._sidebar.file_stack, backend, owner)
            browser.set_interface_colors(self._colors, monochrome=self._monochrome)
            browser.transfer_changed.connect(
                lambda transfer: self._sidebar.transfers.update_transfer(backend, owner, transfer)
            )
            self._browsers[backend] = browser
            browser.navigate(".")
        return self._browsers[backend]

    def _open_system_files(self, page, profile):
        from termana.terminal.ssh_command import resolve_system_ssh_program
        from termana.terminal.system_sftp_session import SystemSftpSession

        message = QMessageBox(self._window)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWindowTitle(tr("Open System SFTP?"))
        message.setText(
            tr(
                "Open a separate file connection for {p0}?\n{p1}@{p2}:{p3}\n\nSystem OpenSSH will use this tab's host, login, route and identity options. It may ask for a password, key passphrase or other response again in this terminal. The terminal stays connected. No file-password saving, new forwards, automatic reconnect or engine fallback. Verify a new/changed server key in the terminal first.",
                p0=f"{profile.name}",
                p1=f"{profile.username or 'Configured user'}",
                p2=f"{profile.host}",
                p3=f"{profile.port}",
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        message.setDefaultButton(QMessageBox.StandardButton.Cancel)
        try:
            approved = message.exec() == QMessageBox.StandardButton.Yes
        finally:
            message.deleteLater()
        if (
            not approved
            or self._closing
            or not isValid(page)
            or self._active_page() is not page
            or self._system_profile(page) != profile
        ):
            return
        program = resolve_system_ssh_program()
        if program is None:
            self._window.statusBar().showMessage(
                tr("The trusted OS OpenSSH client is unavailable."), 10000
            )
            return
        store = self._window._system_ssh_config_store
        try:
            system_config = (None if store is None else store.prepare_sftp(
                self._workspace.ssh_system_config(page.session_id)))
        except (OSError, RuntimeError, ValueError):
            self._window.statusBar().showMessage(tr('Termana SSH config could not be verified safely; no separate SFTP connection was opened.'),10000)
            return
        provider = SystemSftpSession(profile, program, self,
                                     known_hosts=known_hosts_file_path() if store is None else store.trust,
                                     system_config=system_config)
        self._system_sessions.setdefault(page.backend, []).append(provider)
        page.backend.finished.connect(provider.close)
        provider.started.connect(lambda: self._system_ready(page, provider))
        provider.finished.connect(lambda _code: self._system_finished(page, provider))
        provider.secret_requested.connect(
            lambda request: self._request_file_secret(page, provider, request)
        )
        provider.secret_request_finished.connect(
            lambda request: self._finish_file_secret(provider, request)
        )
        self._window.statusBar().showMessage(tr("Opening a separate System SFTP connection…"), 6000)
        self._window.toggle_sidebar_action.setChecked(True)
        self._file_mode_requested = True
        provider.start()

    def _system_ready(self, page, provider):
        if (
            self._closing
            or not isValid(page)
            or page.session_ended
            or self._provider(page) is not provider
        ):
            provider.close()
            return
        # Respect a newer Connections/collapse choice made while signing in.
        # Readiness must not reopen the sidebar or steal another tab's focus.
        self._sync_active()

    def _system_finished(self, page, provider):
        if self._closing:
            return
        if provider.failure_text:
            self._window.statusBar().showMessage(provider.failure_text, 15000)
            browser = self._browsers.get(provider)
            if browser is not None:
                browser.status.setText(provider.failure_text)
        self._sync_active()

    def _request_file_secret(self, page, provider, request):
        if (
            self._closing
            or not isValid(page)
            or page.session_ended
            or self._provider(page) is not provider
        ):
            provider.cancel_secret(request.request_id)
            return
        self._file_secrets[request.request_id] = (page, provider)
        if not self._workspace.begin_ssh_secret_prompt(
            page.session_id,
            request.request_id,
            request.prompt,
            is_host_password=False,
        ):
            self._file_secrets.pop(request.request_id, None)
            provider.cancel_secret(request.request_id)

    def _finish_file_secret(self, provider, request):
        owner = self._file_secrets.get(request.request_id)
        if owner is not None and owner[1] is provider:
            page, _provider = self._file_secrets.pop(request.request_id)
            if isValid(page):
                self._workspace.finish_ssh_secret_prompt(page.session_id, request.request_id)

    def _submit_file_secret(self, session_id, request_id, secret):
        try:
            owner = self._file_secrets.get(request_id)
            if owner is not None and owner[0].session_id == session_id:
                owner[1].submit_secret(request_id, secret)
        finally:
            secret = ""

    def _cancel_file_secret(self, session_id, request_id):
        owner = self._file_secrets.get(request_id)
        if owner is not None and owner[0].session_id == session_id:
            owner[1].cancel_secret(request_id)

    def _cancel_transfer(self, backend, request_id):
        browser = self._browsers.get(backend)
        if browser is not None and not self._closing:
            browser.cancel_transfer(request_id)

    def _forget_page(self, page):
        watched = self._watched_pages.pop(page, None)
        if watched is None:
            return
        backend, _destroyed = watched
        for provider in self._system_sessions.pop(backend, ()):
            provider.close()
            browser = self._browsers.pop(provider, None)
            if browser is not None:
                browser.dispose()
                self._sidebar.remove_browser(browser)
                schedule_delete_later(browser)
        self._auto_open_seen.discard(backend)
        if isValid(backend):
            backend.started.disconnect(self._sync_active)
            backend.finished.disconnect(self._sync_active)
        browser = self._browsers.pop(backend, None)
        if browser is not None:
            browser.dispose()
            self._sidebar.remove_browser(browser)
            schedule_delete_later(browser)

    def set_interface_colors(self, colors, *, monochrome=False):
        self._colors, self._monochrome = colors, monochrome
        self._sidebar.set_interface_colors(colors, monochrome=monochrome)
        for browser in self._browsers.values():
            browser.set_interface_colors(colors, monochrome=monochrome)

    def stop(self):
        self._closing = True
        for sessions in self._system_sessions.values():
            for provider in sessions:
                provider.close()
        for request_id, (page, _provider) in tuple(self._file_secrets.items()):
            if isValid(page):
                self._workspace.finish_ssh_secret_prompt(page.session_id, request_id)
        self._file_secrets.clear()
        for page, (backend, destroyed) in tuple(self._watched_pages.items()):
            if isValid(page):
                page.destroyed.disconnect(destroyed)
            if isValid(backend):
                backend.started.disconnect(self._sync_active)
                backend.finished.disconnect(self._sync_active)
        self._watched_pages.clear()
        self._auto_open_seen.clear()
        for browser in tuple(self._browsers.values()):
            browser.dispose()
