"""Keep native folder panels window-modal and retire them before returning."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFileDialog
from shiboken6 import isValid

from termana.qt_lifecycle import schedule_delete_later


def choose_sftp_directory(parent, title):
    dialog = QFileDialog(parent.window(), title)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted or not isValid(parent):
            return ""
        paths = dialog.selectedFiles()
        return paths[0] if len(paths) == 1 else ""
    finally:
        # Retire the native panel explicitly. Never raise/activate Termana over
        # another app as a side effect of a late dialog callback.
        if isValid(dialog):
            dialog.close()
            schedule_delete_later(dialog)
