"""Dismissible application guidance, kept outside remote-controlled terminal output."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QStyle, QToolButton

from termana.i18n import tr
from termana.widgets.wrapping_label import WrappingLabel


class ConnectionAdvisory(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("connectionAdvisory")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 10, 10)
        layout.setSpacing(10)
        self.message = WrappingLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setMinimumWidth(0)
        self.message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.message.setAccessibleName(tr("SSH connection guidance"))
        self.dismiss_button = QToolButton()
        self.dismiss_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton)
        )
        self.dismiss_button.setAccessibleName(tr("Dismiss SSH connection guidance"))
        self.dismiss_button.setToolTip(tr("Dismiss this guidance; the connection is unchanged"))
        self.dismiss_button.clicked.connect(self.hide)
        layout.addWidget(self.message, 1)
        layout.addWidget(self.dismiss_button, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()

    def show_message(self, message: str) -> None:
        if not isinstance(message, str) or not message or len(message) > 1000:
            raise ValueError("Connection guidance must be bounded nonempty text")
        # A repeating channel failure must not reopen a dismissed hint.
        if message == self.message.text():
            return
        self.message.setText(message)
        self.show()
