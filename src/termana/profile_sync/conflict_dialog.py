"""Plain-text review before deliberately selecting one complete sync catalog."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)


class ConflictDialog(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resolve connection conflicts")
        self.resize(640, 430)
        self.selection = None
        layout = QVBoxLayout(self)
        warning = QLabel(
            "Choose one complete catalog. Edits present only in the other version will be discarded. "
            "Deleted connections will not be restored. Cancel keeps both versions unchanged.",
            self,
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        tabs = QTabWidget(self)
        for title, snapshot in (("This computer", report.local), ("GitHub", report.remote)):
            text = QPlainTextEdit(self)
            text.setReadOnly(True)
            rows = []
            for row in snapshot.records:
                data = row.document()
                if row.kind == "folder":
                    rows.append("Folder: " + data["name"])
                else:
                    rows.append(
                        f"{data['name']}  ·  {data['protocol']}  ·  {data['host']}:{data['port']}"
                    )
            text.setPlainText("\n".join(rows) or "Empty catalog")
            tabs.addTab(text, title)
        layout.addWidget(tabs)
        box = QDialogButtonBox(self)
        for title, selection in (
            ("Keep this computer", "local"),
            ("Use GitHub", "remote"),
            ("Cancel", None),
        ):
            button = QPushButton(title, self)
            button.setAutoDefault(False)
            button.setProperty("actionRole", "secondary")
            button.clicked.connect(lambda _checked=False, side=selection: self._choose(side))
            box.addButton(button, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(box)

    def _choose(self, selection):
        self.selection = selection
        self.done(
            QDialog.DialogCode.Accepted if selection is not None else QDialog.DialogCode.Rejected
        )
