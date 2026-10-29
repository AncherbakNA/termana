"""Packaged affected-UX checks with synthetic input and disposable configuration."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from termana.dialogs.paste_preview_dialog import PastePreviewDialog
from termana.dialogs.settings_dialog import SettingsDialog
from termana.models.connection import ConnectionCatalog, ConnectionProfile
from termana.models.settings import ApplicationSettings
from termana.storage.configuration_backup import apply_pending_restore, create_backup, stage_restore
from termana.storage.connection_store import ConnectionStore
from termana.storage.settings_store import SettingsStore
from termana.terminal.backend import TerminalBackend
from termana.terminal.input_encoder import TerminalInputEncoder
from termana.widgets.terminal_workspace import TerminalWorkspace


class _OwnedBackend(TerminalBackend):
    """No processes, network, secrets or user shell startup files."""

    def __init__(self):
        super().__init__()
        self.running = False
        self.writes = []

    is_running = property(lambda self: self.running)
    text_encoding = property(lambda self: "utf-8")
    echoes_input = property(lambda self: True)
    supports_direct_input = property(lambda self: True)

    def start(self):
        self.running = True
        self.started.emit()

    def write(self, data):
        self.writes.append(data)

    def resize(self, columns, rows):
        pass

    def close(self):
        if self.running:
            self.running = False
            self.finished.emit(0)


def _settle(app):
    for _ in range(8):
        app.processEvents()


def run(app):
    """Run within the explicit --check-runtime process, never normal startup."""
    checks = []
    with TemporaryDirectory(prefix="termana-packaged-fixture-") as temporary:
        directory = Path(temporary)
        path = directory / "settings.json"
        store = SettingsStore(path)
        store.save(ApplicationSettings())
        legacy = json.loads(path.read_bytes())
        legacy["schema_version"] = 13
        for name in ("local_terminal", "backspace_sends_ctrl_h", "toolbar_actions"):
            del legacy["settings"][name]
        legacy["settings"]["click_edit_gesture"] = "alt"
        raw = json.dumps(legacy).encode()
        path.write_bytes(raw)
        assert store.load() == ApplicationSettings() and path.read_bytes() == raw
        connection_path = directory / "connections.json"
        connections = ConnectionStore(connection_path)
        catalog = ConnectionCatalog().with_profile_added(ConnectionProfile("Migration fixture", "test.invalid"))
        connections.save(catalog)
        legacy_connections = json.loads(connection_path.read_bytes())
        legacy_connections["schema_version"] = 14
        legacy_connections.pop("sync_state")
        for profile in legacy_connections["connections"]:
            del profile["accent_color"]
        connection_raw = json.dumps(legacy_connections).encode()
        connection_path.write_bytes(connection_raw)
        assert connections.load() == catalog and connection_path.read_bytes() == connection_raw
        archive = directory / "pre-update.zip"
        create_backup(directory, archive)
        store.save(ApplicationSettings())
        assert json.loads(path.read_bytes())["schema_version"] == 15
        connections.save(catalog)
        assert json.loads(connection_path.read_bytes())["schema_version"] == 16
        stage_restore(directory, archive)
        assert apply_pending_restore(directory) is not None and path.read_bytes() == raw
        assert connection_path.read_bytes() == connection_raw and connections.load() == catalog
        checks.append("legacy-settings13-connections14-readonly-settings15-connections16-save-matching-rollback-fixture")

        backends = []

        def factory(_command):
            backend = _OwnedBackend()
            backends.append(backend)
            return backend

        workspace = TerminalWorkspace(factory)
        settings = SettingsDialog(settings=ApplicationSettings())
        preview = PastePreviewDialog("echo one\necho two\n", unprotected=True)
        try:
            workspace.resize(900, 450)
            workspace.show()
            workspace.activateWindow()
            workspace.open_local_terminal()
            _settle(app)
            page = workspace.tabs.currentWidget()
            label = workspace.tabs.tabText(workspace.tabs.currentIndex())
            assert label == str(Path.home()).replace("&", "&&")
            checks.append("initial-home-tab-label")
            encoder = page.output_view._input_encoder = TerminalInputEncoder()
            modifier = (
                Qt.KeyboardModifier.AltModifier
                if encoder.is_macos
                else Qt.KeyboardModifier.ControlModifier
            )
            QApplication.sendEvent(
                page.output_view, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, modifier)
            )
            expected = b"\x1bb" if encoder.is_macos else b"\x1b[1;5D"
            assert backends[0].writes == [expected]
            assert not hasattr(page.output_view, "click_edit")
            checks.append("word-navigation-input-routing")
            page.output_view.setFocus()
            _settle(app)
            assert page.output_view.hasFocus()
            checks.append("native-qt-terminal-focus")
            assert workspace.split_down()
            assert workspace.focus_next_pane()
            assert workspace.move_current_to_next_pane()
            assert workspace.tabs.currentWidget() is page and page.backend is backends[0]
            assert backends[0].is_running and len(backends) == 1
            checks.append("pane-move-preserves-backend")

            preview.show()
            _settle(app)
            assert not preview.preview.isReadOnly()
            preview.preview.setPlainText("echo edited\n")
            assert preview.preview.toPlainText() == "echo edited\n"
            checks.append("editable-numbered-paste-preview")
            settings.show()
            settings.reveal_control(settings.confirm_session_close_input)
            _settle(app)
            assert not hasattr(settings, "click_edit_gesture_input")
            assert not settings.confirm_session_close_input.isChecked()
            assert settings.findChild(QDialogButtonBox) is not None
            checks.append("preferences-without-click-placement")
        finally:
            preview.close()
            settings.close()
            workspace.shutdown_all()
            workspace.close()
            for widget in (preview, settings, workspace):
                widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return checks
