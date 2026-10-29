"""Explicit Python runtime check: no user stores, keychain access or remote hosts."""

import importlib
import json
import platform
import sys
from pathlib import Path


def main(report_path: str) -> int:
    """Write a new report after bounded, isolated GUI and terminal checks."""
    from termana.build_info import packaged_build

    report = Path(report_path)
    try:
        stream = report.open("x", encoding="utf-8")
    except OSError:
        print(
            "Cannot create the runtime report. Choose a new file in a writable folder.",
            file=sys.stderr,
        )
        return 2
    with stream:
        result = {
            "passed": False,
            "platform": sys.platform,
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "scope": "GUI/helper/system SSH/keyring import plus affected UX and disposable settings update/rollback fixtures; no physical input claim",
            "user_data_accessed": False,
            "remote_connections": False,
            "build_id": packaged_build()["build_id"],
        }
        try:
            result.update(_run())
        except Exception as error:
            # Do not serialize arbitrary exception strings, environment or user paths.
            result["errors"] = [f"Runtime check failed ({type(error).__name__})."]
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(
        "Runtime check passed." if result["passed"] else "Runtime check failed; review the report."
    )
    return 0 if result["passed"] else 1


def _run() -> dict:
    from PySide6.QtCore import QEventLoop, QTimer, qVersion
    from PySide6.QtWidgets import QApplication

    from termana.helper_runtime import helper_command
    from termana.main_window import MainWindow
    from termana.terminal.backend_factory import create_terminal_backend
    from termana.terminal.command import ShellCommand
    from termana.terminal.legacy_ssh import (
        _REVIEWED_FRONTENDS,
        legacy_helper_path,
        verify_legacy_frontend,
    )
    from termana.terminal.ssh_command import resolve_system_ssh_program
    from termana.theme import DEFAULT_THEME, build_stylesheet

    app = QApplication([sys.argv[0]])
    app.setApplicationName("Termana Runtime Check")
    app.setStyleSheet(build_stylesheet(DEFAULT_THEME))
    ssh = resolve_system_ssh_program()
    if ssh is None:
        return {
            "passed": False,
            "errors": ["System OpenSSH client is missing."],
            "qt_version": qVersion(),
        }
    keyring_backend = {"darwin": "macOS", "win32": "Windows", "linux": "SecretService"}[
        sys.platform
    ]
    importlib.import_module(f"keyring.backends.{keyring_backend}")
    legacy_verified = None
    if (sys.platform, platform.machine()) in _REVIEWED_FRONTENDS:
        verify_legacy_frontend(legacy_helper_path())
        legacy_verified = True
    # An explicit Python command avoids user shell startup files and environment scripts.
    command = helper_command(
        "probe",
        ShellCommand(
            sys.executable,
            ("-u", "-c", "print('TERMANA_RUNTIME_OK', flush=True)"),
            (("PYTHONIOENCODING", "utf-8"),),
        ),
    )
    terminal = create_terminal_backend(command)
    ssh_backend = create_terminal_backend(ShellCommand(ssh, ("-V",)))
    output = [bytearray(), bytearray()]
    exits, errors = {}, []
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)

    def finished(index, code):
        exits[index] = code
        if len(exits) == 2:
            QTimer.singleShot(150, loop.quit)

    def collect(index, data):
        remaining = 16 * 1024 - len(output[index])
        output[index].extend(data[: max(0, remaining)])
        if len(data) > remaining and "Excessive test output." not in errors:
            errors.append("Excessive test output.")

    for index, backend in enumerate((terminal, ssh_backend)):
        backend.data_received.connect(lambda data, i=index: collect(i, data))
        backend.error_occurred.connect(lambda _message: errors.append("Terminal backend error."))
        backend.finished.connect(lambda code, i=index: finished(i, code))
    window = MainWindow(terminal_backend_factory=lambda _command: terminal)
    window.setEnabled(False)
    try:
        window.show()
        window.terminal_workspace.open_local_terminal()
        ssh_backend.start()
        timer.timeout.connect(lambda: (errors.append("Runtime check timed out."), loop.quit()))
        timer.start(15000)
        loop.exec()
        timer.stop()
        page = window.terminal_workspace.tabs.currentWidget()
        document = "\n".join(
            page.screen.document_line_text(row) for row in range(page.screen.document_rows)
        )
        retained = "TERMANA_RUNTIME_OK" in document
        passed = not errors and exits == {0: 0, 1: 0} and retained and b"OpenSSH" in output[1]
        window.hide()
        from termana.runtime_ux_check import run as run_ux_checks

        ux_checks = run_ux_checks(app) if passed else []
        return {
            "passed": passed,
            "qt_version": qVersion(),
            "errors": errors,
            "terminal_exit": exits.get(0),
            "ssh_exit": exits.get(1),
            "terminal_marker_retained": retained,
            "ssh_version": bytes(output[1]).decode("utf-8", errors="replace").strip(),
            "checks": ["main_window", "python_terminal_helper", "system_ssh", "keyring_import"],
            "affected_ux_checks": ux_checks,
            "qt_platform": app.platformName(),
            "ssh1_component_verified": legacy_verified,
        }
    finally:
        timer.stop()
        for backend in (terminal, ssh_backend):
            backend.close()
        if any(backend.is_running for backend in (terminal, ssh_backend)):
            shutdown = QEventLoop()
            QTimer.singleShot(3000, shutdown.quit)
            for backend in (terminal, ssh_backend):
                backend.finished.connect(
                    lambda _code: (
                        shutdown.quit()
                        if not any(item.is_running for item in (terminal, ssh_backend))
                        else None
                    )
                )
            shutdown.exec()
        window.hide()
