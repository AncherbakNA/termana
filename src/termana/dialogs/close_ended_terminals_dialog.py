"""Explicit review of a frozen set of ended tabs, without backend access."""

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.open_terminal import OpenTerminalSession, OpenTerminalState
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.wrapping_label import WrappingLabel


class CloseEndedTerminalsDialog(QDialog):
    """Review names, not terminal output; Enter/Escape keep the history intact."""

    def __init__(self, parent: QWidget, *, sessions: tuple[OpenTerminalSession, ...]) -> None:
        if not sessions or any(
            session.state is not OpenTerminalState.ENDED for session in sessions
        ):
            raise ValueError("Review requires ended terminal snapshots")
        super().__init__(parent)
        self.session_ids = tuple(session.session_id for session in sessions)
        self.setObjectName("closeEndedTerminalsDialog")
        self.setWindowTitle(tr("Close ended terminals?"))
        self.setSizeGripEnabled(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("settingsScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("settingsScrollContent")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        count = len(sessions)
        subject = (
            tr("this ended terminal")
            if count == 1
            else tr("these {p0} ended terminals", p0=f"{count}")
        )
        self.notice = WrappingLabel(
            tr(
                "Close {p0}? Retained output will be lost. Cancel and use Save output first if you need to keep it. Saved connection profiles and passwords are unchanged.",
                p0=f"{subject}",
            )
        )
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.setMinimumWidth(0)
        layout.addWidget(self.notice)
        self.session_list = QListWidget()
        self.session_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.session_list.setAccessibleName(tr("Ended terminals selected for closing"))
        self.session_list.setUniformItemSizes(True)
        self.session_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.session_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.session_list.setMinimumWidth(0)
        for session in sessions:
            text = tr(
                "{p0}\nPane {p1} · {p2}",
                p0=f"{session.title}",
                p1=f"{session.pane_number}",
                p2=f"{session.session_name}",
            )
            item = QListWidgetItem(text)
            item.setToolTip(plain_text_tooltip(text))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, text.replace("\n", ", "))
            self.session_list.addItem(item)
        layout.addWidget(self.session_list, 1)
        self.scope_notice = WrappingLabel(
            tr(
                "Only this reviewed list is affected. Hidden or newly ended terminals are excluded. Terminals no longer ended or awaiting reconnect will be skipped."
            )
        )
        self.scope_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.scope_notice.setWordWrap(True)
        self.scope_notice.setMinimumWidth(0)
        layout.addWidget(self.scope_notice)
        self.scroll_area.setWidget(body)
        root.addWidget(self.scroll_area, 1)
        self.footer = QWidget()
        self.footer.setObjectName("settingsDialogFooter")
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(24, 12, 24, 20)
        self.button_box = QDialogButtonBox()
        self.cancel_button = QPushButton(tr("Keep terminals"))
        self.cancel_button.setProperty("actionRole", "primary")
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.close_button = QPushButton(tr("Close ended terminals"))
        self.close_button.setObjectName("confirmCloseEndedTerminalsButton")
        self.close_button.setProperty("actionRole", "danger")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.accept)
        self.button_box.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.button_box.addButton(self.close_button, QDialogButtonBox.ButtonRole.DestructiveRole)
        footer_layout.addWidget(self.button_box)
        root.addWidget(self.footer)
        for widget in (
            self.notice,
            self.session_list,
            self.scope_notice,
            self.cancel_button,
            self.close_button,
        ):
            scale_widget_font(widget, 1.0)
        self._layout_pending = False
        self.button_box.installEventFilter(self)
        self.resize(600, 480)
        self._adapt_layout()
        self.cancel_button.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.modifiers():
                self.reject()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._adapt_layout()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.button_box
            and event.type()
            in (QEvent.Type.LayoutRequest, QEvent.Type.FontChange, QEvent.Type.StyleChange)
            and not self._layout_pending
        ):
            self._layout_pending = True
            QTimer.singleShot(0, self, self._adapt_layout)
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            QTimer.singleShot(0, self, self._adapt_layout)

    def _adapt_layout(self) -> None:
        self._layout_pending = False
        if not hasattr(self, "button_box"):
            return
        self.session_list.setMinimumHeight(4 * self.session_list.fontMetrics().height() + 16)
        required = sum(button.sizeHint().width() for button in self.button_box.buttons()) + 24
        self.button_box.setOrientation(
            Qt.Orientation.Vertical if required > self.width() - 48 else Qt.Orientation.Horizontal
        )
        available = self.screen().availableGeometry()
        self.setMinimumSize(min(360, available.width() - 48), min(300, available.height() - 80))
        self.setMaximumSize(max(1, available.width() - 48), max(1, available.height() - 80))
