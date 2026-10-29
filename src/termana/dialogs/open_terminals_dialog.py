"""A searchable, presentation-only picker for immutable terminal snapshots."""

from __future__ import annotations

import unicodedata
from uuid import UUID

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeyEvent, QKeySequence, QResizeEvent, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import count_noun, tr
from termana.models.open_terminal import OpenTerminalSession, OpenTerminalState
from termana.models.terminal_layout import MAX_TERMINAL_PANES
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.form_controls import FormComboBox
from termana.widgets.wrapping_label import WrappingLabel

_MAX_VISIBLE_TEXT = 512


def _safe_text(value: str) -> str:
    """Keep metadata bounded and prevent controls from disguising a row's identity."""
    return "".join(
        "\N{REPLACEMENT CHARACTER}"
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        else character
        for character in value[:_MAX_VISIBLE_TEXT]
    )


def _normalize(value: str) -> str:
    """Use literal compatibility-normalized, caseless words, never a regex."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _details(session: OpenTerminalSession) -> str:
    parts = [
        tr("Pane {p0}", p0=f"{session.pane_number}"),
        tr(session.state.value, context="terminal-state"),
    ]
    if session.active:
        parts.append(tr("Current"))
    if session.attention:
        parts.append(tr("Attention"))
    return " · ".join(parts)


class OpenTerminalsDialog(QDialog):
    """Request an exact UUID; only the live owner may approve and close the picker."""

    session_requested = Signal(object)
    close_ended_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        sessions: tuple[OpenTerminalSession, ...],
    ) -> None:
        super().__init__(parent)
        self._sessions: tuple[OpenTerminalSession, ...] | None = None
        self._search_documents: dict[UUID, str] = {}
        self._visible_ended_sessions: tuple[OpenTerminalSession, ...] = ()
        self.setObjectName("openTerminalsDialog")
        self.setWindowTitle(tr("Open terminals"))
        self.setAccessibleName(tr("Open terminals"))
        self.setSizeGripEnabled(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # The footer remains reachable even with very large application fonts.
        # Rows use the native item delegate; no row owns a child widget.
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("settingsScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("settingsScrollContent")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 20, 24, 12)
        layout.setSpacing(12)
        title = self._label(tr("Open terminals"), "dialogTitle")
        scale_widget_font(title, 1.45, weight=QFont.Weight.Bold)
        layout.addWidget(title)

        self.search_input = QLineEdit()
        scale_widget_font(self.search_input, 1.0)
        self.search_input.setObjectName("openTerminalSearch")
        self.search_input.setPlaceholderText(tr("Search by name, pane, or status"))
        self.search_input.setAccessibleName(tr("Search open terminals"))
        self.search_input.setAccessibleDescription(
            tr(
                "Search terminal titles, original session names, panes, and status. Use Up and Down to select, then Enter to switch."
            )
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMaxLength(512)
        layout.addWidget(self.search_input)

        filters = QHBoxLayout()
        self.status_filter = FormComboBox()
        self.status_filter.setAccessibleName(tr("Filter terminals by status"))
        self.status_filter.addItem(tr("All statuses"), None)
        for state in OpenTerminalState:
            self.status_filter.addItem(tr(state.value, context="terminal-state"), state.value)
        self.status_filter.addItem(tr("Needs attention"), "attention")
        self.pane_filter = FormComboBox()
        self.pane_filter.setAccessibleName(tr("Filter terminals by pane"))
        self.pane_filter.addItem(tr("All panes"), None)
        for number in range(1, MAX_TERMINAL_PANES + 1):
            self.pane_filter.addItem(tr("Pane {p0}", p0=f"{number}"), number)
        for combo in (self.status_filter, self.pane_filter):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            filters.addWidget(combo, 1)
        layout.addLayout(filters)

        self.session_list = QListWidget()
        scale_widget_font(self.session_list, 1.0)
        self.session_list.setObjectName("openTerminalList")
        self.session_list.setAccessibleName(tr("Open terminals"))
        self.session_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.session_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.session_list.setUniformItemSizes(True)
        self.session_list.setWordWrap(False)
        self.session_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.session_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.session_list.setMinimumWidth(0)

        self.empty_label = self._label("", "openTerminalsEmptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_stack = QStackedWidget()
        self.state_stack.setMinimumWidth(0)
        self.state_stack.addWidget(self.session_list)
        self.state_stack.addWidget(self.empty_label)
        layout.addWidget(self.state_stack, 1)

        self.count_label = self._label("", "mutedText")
        layout.addWidget(self.count_label)
        self.result_label = self._label("", "openTerminalsResult")
        self.result_label.hide()
        layout.addWidget(self.result_label)
        self.error_label = self._label("", "settingsError")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.scroll_area.setWidget(body)
        root.addWidget(self.scroll_area, 1)

        self.footer = QWidget()
        self.footer.setObjectName("settingsDialogFooter")
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(24, 12, 24, 20)
        self.button_box = QDialogButtonBox()
        self.button_box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._layout_pending = False
        self.cancel_button = QPushButton(tr("Cancel"))
        scale_widget_font(self.cancel_button, 1.0)
        self.cancel_button.setObjectName("cancelOpenTerminalsButton")
        self.cancel_button.setProperty("actionRole", "secondary")
        self.cancel_button.setAutoDefault(False)
        self.button_box.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.close_ended_button = QPushButton(tr("Close ended…"))
        scale_widget_font(self.close_ended_button, 1.0)
        self.close_ended_button.setObjectName("closeEndedTerminalsButton")
        self.close_ended_button.setProperty("actionRole", "secondary")
        self.close_ended_button.setAutoDefault(False)
        self.close_ended_button.setToolTip(
            tr(
                "Review closing only ended terminals matching the current search and filters. Their retained output will be lost. Running terminals are never stopped."
            )
        )
        self.button_box.addButton(self.close_ended_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.switch_button = QPushButton(tr("Switch"))
        scale_widget_font(self.switch_button, 1.0)
        self.switch_button.setObjectName("switchTerminalButton")
        self.switch_button.setProperty("actionRole", "primary")
        self.switch_button.setAutoDefault(False)
        # ActionRole deliberately does not emit the button box's accepted signal.
        self.button_box.addButton(self.switch_button, QDialogButtonBox.ButtonRole.ActionRole)
        footer_layout.addWidget(self.button_box)
        root.addWidget(self.footer)

        self.search_input.textChanged.connect(self._filter_changed)
        self.status_filter.currentIndexChanged.connect(self._filter_changed)
        self.pane_filter.currentIndexChanged.connect(self._filter_changed)
        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self.find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.find_shortcut.activated.connect(self._find)
        self.session_list.itemSelectionChanged.connect(self._selection_changed)
        self.session_list.itemDoubleClicked.connect(self._request_selected)
        self.switch_button.clicked.connect(self._request_selected)
        self.close_ended_button.clicked.connect(self._request_close_ended)
        self.cancel_button.clicked.connect(self.reject)
        QWidget.setTabOrder(self.search_input, self.status_filter)
        QWidget.setTabOrder(self.status_filter, self.pane_filter)
        QWidget.setTabOrder(self.pane_filter, self.session_list)
        QWidget.setTabOrder(self.session_list, self.switch_button)
        QWidget.setTabOrder(self.switch_button, self.close_ended_button)
        QWidget.setTabOrder(self.close_ended_button, self.cancel_button)

        self.set_sessions(sessions)
        self._adapt_layout()
        available = self.screen().availableGeometry()
        self.resize(min(660, available.width() - 48), min(520, available.height() - 80))
        self.search_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.search_input.installEventFilter(self)
        self.session_list.installEventFilter(self)
        self.button_box.installEventFilter(self)

    @property
    def visible_ended_sessions(self) -> tuple[OpenTerminalSession, ...]:
        """The displayed ended subset, never rows hidden by a filter."""
        return self._visible_ended_sessions

    @property
    def selected_session_id(self) -> UUID | None:
        """Return a selected visible row, never a stale row position or tab index."""
        item = self.session_list.currentItem()
        if item is None or not item.isSelected():
            return None
        session_id = item.data(Qt.ItemDataRole.UserRole)
        return session_id if isinstance(session_id, UUID) else None

    def set_sessions(self, sessions: tuple[OpenTerminalSession, ...]) -> None:
        """Refresh metadata without changing selection to an unrelated terminal."""
        sessions = tuple(sessions)
        if sessions == self._sessions:
            return
        initial = self._sessions is None
        selected_id = self.selected_session_id
        self._sessions = sessions
        self._search_documents = {
            session.session_id: _normalize(
                " ".join((session.title, session.session_name, _details(session)))
            )
            for session in sessions
        }
        if initial:
            selected_id = next(
                (session.session_id for session in sessions if session.active),
                sessions[0].session_id if sessions else None,
            )
        self._rebuild_rows(selected_id, select_first=initial)

    def show_error(self, message: str) -> None:
        """Present a bounded, inert owner-supplied failure without accepting."""
        text = _safe_text(message)
        self.error_label.setText(text)
        self.error_label.setAccessibleName(text)
        self.error_label.setVisible(bool(text))

    def show_result(self, message: str) -> None:
        """Report reviewed cleanup without presenting success as an error."""
        text = _safe_text(message)
        self.result_label.setText(text)
        self.result_label.setAccessibleName(text)
        self.result_label.setVisible(bool(text))

    def _filter_changed(self) -> None:
        self.show_error("")
        self.show_result("")
        for combo in (self.status_filter, self.pane_filter):
            combo.setToolTip(plain_text_tooltip(combo.currentText()))
        self._rebuild_rows(self.selected_session_id, select_first=True)

    def _rebuild_rows(self, selected_id: UUID | None, *, select_first: bool) -> None:
        terms = _normalize(self.search_input.text()).split()
        sessions = self._sessions or ()
        status = self.status_filter.currentData()
        pane = self.pane_filter.currentData()
        matching = tuple(
            session
            for session in sessions
            if all(term in self._search_documents[session.session_id] for term in terms)
            and (pane is None or session.pane_number == pane)
            and (
                status is None
                or session.state.value == status
                or (status == "attention" and session.attention)
            )
        )
        self._visible_ended_sessions = tuple(
            session for session in matching if session.state is OpenTerminalState.ENDED
        )
        count = len(self._visible_ended_sessions)
        self.close_ended_button.setEnabled(bool(count))
        self.close_ended_button.setAccessibleName(
            tr("Review closing {p0} visible ended terminals", p0=f"{count}")
        )
        selected_item = None
        with QSignalBlocker(self.session_list):
            self.session_list.clear()
            for session in matching:
                text = f"{_safe_text(session.title)}\n{_details(session)}"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, session.session_id)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, text.replace("\n", ", "))
                item.setToolTip(
                    plain_text_tooltip(
                        tr(
                            "Title: {p0}\nSession: {p1}\n{p2}",
                            p0=f"{_safe_text(session.title)}",
                            p1=f"{_safe_text(session.session_name)}",
                            p2=f"{_details(session)}",
                        )
                    )
                )
                self.session_list.addItem(item)
                if session.session_id == selected_id:
                    selected_item = item
            if selected_item is None and matching and select_first:
                selected_item = self.session_list.item(0)
            self.session_list.setCurrentItem(selected_item)
            if selected_item is None:
                self.session_list.clearSelection()
            else:
                self.session_list.scrollToItem(selected_item)

        total = len(sessions)
        noun = count_noun("terminal", "terminals", total)
        self.count_label.setText(
            tr("{p0} of {p1} {p2}", p0=f"{len(matching)}", p1=f"{total}", p2=f"{noun}")
            if terms or status is not None or pane is not None
            else f"{total} {noun}"
        )
        self.empty_label.setText(
            tr(
                "No matching terminals.\nTry another name, pane, or status. Escape resets the filters."
            )
            if sessions
            else tr("No open terminals.\nOpen a terminal to see it here.")
        )
        self.state_stack.setCurrentWidget(self.session_list if matching else self.empty_label)
        self._selection_changed()

    def _selection_changed(self) -> None:
        self.switch_button.setEnabled(self.selected_session_id is not None)

    def _request_selected(self) -> None:
        session_id = self.selected_session_id
        if session_id is not None:
            self.session_requested.emit(session_id)

    def _request_close_ended(self) -> None:
        if self._visible_ended_sessions:
            self.close_ended_requested.emit(
                tuple(session.session_id for session in self._visible_ended_sessions)
            )

    def _move_selection(self, direction: int) -> None:
        count = self.session_list.count()
        if count:
            row = self.session_list.currentRow()
            row = (0 if direction > 0 else count - 1) if row < 0 else row + direction
            self.session_list.setCurrentRow(max(0, min(count - 1, row)))
            self.session_list.scrollToItem(self.session_list.currentItem())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.button_box
            and event.type()
            in (
                QEvent.Type.LayoutRequest,
                QEvent.Type.FontChange,
                QEvent.Type.StyleChange,
            )
            and not self._layout_pending
        ):
            self._layout_pending = True
            QTimer.singleShot(0, self, self._adapt_layout)
        if (
            watched in (self.search_input, self.session_list)
            and event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
        ):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                    self._request_selected()
                return True
            if event.key() == Qt.Key.Key_Escape and not event.modifiers():
                if not self._clear_filters():
                    self.reject()
                return True
            if (
                watched is self.search_input
                and not event.modifiers()
                and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)
            ):
                self._move_selection(1 if event.key() == Qt.Key.Key_Down else -1)
                return True
            if watched is self.session_list and not event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            ):
                if event.text() and event.text().isprintable():
                    self.search_input.setFocus(Qt.FocusReason.OtherFocusReason)
                    self.search_input.insert(event.text())
                    return True
                if event.key() == Qt.Key.Key_Backspace:
                    self.search_input.setFocus(Qt.FocusReason.OtherFocusReason)
                    self.search_input.backspace()
                    return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # No QDialog default-button acceptance path may bypass live UUID validation.
        if event.key() == Qt.Key.Key_Escape and not event.modifiers() and self._clear_filters():
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers():
                event.accept()
                return
            if self.cancel_button.hasFocus():
                self.reject()
            elif self.close_ended_button.hasFocus():
                self._request_close_ended()
            else:
                self._request_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def _find(self) -> None:
        self._focus_search()
        self.search_input.selectAll()

    def _clear_filters(self) -> bool:
        if not (
            self.search_input.text()
            or self.status_filter.currentIndex()
            or self.pane_filter.currentIndex()
        ):
            return False
        with (
            QSignalBlocker(self.search_input),
            QSignalBlocker(self.status_filter),
            QSignalBlocker(self.pane_filter),
        ):
            self.search_input.clear()
            self.status_filter.setCurrentIndex(0)
            self.pane_filter.setCurrentIndex(0)
        self._filter_changed()
        self._focus_search()
        return True

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self, self._focus_search)

    def _focus_search(self) -> None:
        if self.isVisible():
            self.search_input.setFocus(Qt.FocusReason.OtherFocusReason)
            self.scroll_area.ensureWidgetVisible(self.search_input)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._adapt_layout()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            QTimer.singleShot(0, self, self._adapt_layout)

    def _adapt_layout(self) -> None:
        self._layout_pending = False
        if not hasattr(self, "button_box"):
            return
        self.session_list.setMinimumHeight(4 * self.session_list.fontMetrics().height() + 16)
        required = sum(button.sizeHint().width() for button in self.button_box.buttons()) + 36
        self.button_box.setOrientation(
            Qt.Orientation.Vertical if required > self.width() - 48 else Qt.Orientation.Horizontal
        )
        available = self.screen().availableGeometry()
        self.setMinimumSize(min(360, available.width() - 48), min(280, available.height() - 80))
        self.setMaximumSize(max(1, available.width() - 48), max(1, available.height() - 80))

    @staticmethod
    def _label(text: str, name: str) -> QLabel:
        label = WrappingLabel(text)
        scale_widget_font(label, 1.0)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return label
