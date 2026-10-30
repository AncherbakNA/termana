"""Isolated installer preflight: no user stores, network, vault or focus tests."""

import json
from pathlib import Path


def main(report):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QWidget

    result = {
        "passed": False,
        "user_data_accessed": False,
        "remote_connections": False,
        "scope": "update startup: imports, native Qt handle, pinned RDP component",
    }
    from termana.build_info import application_version

    result["version"] = application_version()
    try:
        from termana.credentials.embedded_ssh_auth import require_embedded_auth_contract
        from termana.main_window import MainWindow  # noqa: F401
        from termana.rdp.component import verified_component

        require_embedded_auth_contract()
        verified_component()
        app = QApplication([])
        if app.platformName() not in {"cocoa", "windows", "xcb", "wayland"}:
            raise RuntimeError()
        widget = QWidget()
        widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        if not widget.winId():
            raise RuntimeError()
        QTimer.singleShot(0, app.quit)
        app.exec()
        widget.close()
        result["passed"] = True
        result["qt_platform"] = app.platformName()
    except Exception:
        result["message"] = "The application cannot initialize its update runtime on this computer"
    try:
        with Path(report).open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
    except OSError:
        return 2
    return 0 if result["passed"] else 1
