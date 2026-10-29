"""Explicit, ephemeral output search; browsing a result never switches a tab."""

from uuid import UUID

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.open_terminal import OpenTerminalSession, OpenTerminalState
from termana.models.terminal_multi_search import (
    MAX_MULTI_SEARCH_QUERY,
    MAX_MULTI_SEARCH_SESSIONS,
    TerminalMultiSearchResult,
    TerminalSearchHit,
)
from termana.widgets.themed_checkbox import ThemedCheckBox


class TerminalMultiSearchDialog(QDialog):
    search_requested = Signal()
    refresh_requested = Signal()
    result_requested = Signal(object)

    def __init__(self, parent: QWidget, *, sessions: tuple[OpenTerminalSession, ...]):
        super().__init__(parent)
        self.setObjectName("terminalMultiSearchDialog")
        self.setWindowTitle(tr("Find in open terminals"))
        self.setSizeGripEnabled(True)
        self._hits = ()
        self._busy = False
        self._scope_initialized = False
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        notice = QLabel(
            tr(
                "Search selected terminals' captured output. Output may contain sensitive data. Nothing is saved or copied. Snapshots omit trailing padding spaces; rows are not live navigation coordinates."
            )
        )
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.query_input = QLineEdit()
        self.query_input.setAccessibleName(tr("Literal terminal output query"))
        self.query_input.setPlaceholderText(tr("Literal text to find"))
        self.query_input.setMaxLength(MAX_MULTI_SEARCH_QUERY + 1)
        self.query_input.returnPressed.connect(self.search_requested)
        layout.addWidget(self.query_input)
        self.case_sensitive = ThemedCheckBox(tr("Case sensitive"))
        self.whole_words = ThemedCheckBox(tr("Whole words"))
        layout.addWidget(self.case_sensitive)
        layout.addWidget(self.whole_words)
        scope_label = QLabel(
            tr("Check 1-{p0} terminals, then Search:", p0=f"{MAX_MULTI_SEARCH_SESSIONS}")
        )
        layout.addWidget(scope_label)
        self.scope_list = QListWidget()
        self.scope_list.setAccessibleName(tr("Explicit terminal search scope"))
        self.scope_list.setMaximumHeight(150)
        self.scope_list.setMinimumHeight(80)
        self.scope_list.setUniformItemSizes(True)
        self.scope_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.scope_list)
        self.refresh_button = QPushButton(tr("Refresh terminal list"))
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.clicked.connect(self.refresh_requested)
        layout.addWidget(self.refresh_button)
        self.search_button = QPushButton(tr("Search snapshots"))
        self.search_button.setAutoDefault(False)
        self.search_button.clicked.connect(self.search_requested)
        layout.addWidget(self.search_button)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName(tr("Terminal search result status"))
        layout.addWidget(self.status_label)
        self.result_tree = QTreeWidget()
        self.result_tree.setAccessibleName(tr("Captured terminal matching rows"))
        self.result_tree.setHeaderLabels((tr("Terminal"), tr("Snapshot row"), tr("Text preview")))
        self.result_tree.setRootIsDecorated(False)
        self.result_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.result_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_tree.setUniformRowHeights(True)
        self.result_tree.setMinimumHeight(160)
        self.result_tree.itemSelectionChanged.connect(self._update_switch)
        self.result_tree.itemActivated.connect(lambda *_: self._request_selected())
        layout.addWidget(self.result_tree, 1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self.button_box = QDialogButtonBox()
        self.switch_button = QPushButton(tr("Switch to terminal"))
        self.switch_button.setAutoDefault(False)
        self.switch_button.clicked.connect(self._request_selected)
        self.close_button = QPushButton(tr("Close"))
        self.close_button.setAutoDefault(False)
        self.button_box.addButton(self.switch_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.button_box.addButton(self.close_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.button_box.rejected.connect(self.reject)
        root.addWidget(self.button_box)
        self.set_sessions(sessions)
        self.query_input.textChanged.connect(self._invalidate_results)
        self.case_sensitive.toggled.connect(self._invalidate_results)
        self.whole_words.toggled.connect(self._invalidate_results)
        self.scope_list.itemChanged.connect(self._invalidate_results)
        self._update_switch()
        self.resize(840, 680)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(840, available.width()), min(680, max(300, available.height() - 80)))

    def set_sessions(self, sessions: tuple[OpenTerminalSession, ...]) -> None:
        previous = set(self.selected_session_ids)
        initialized = self._scope_initialized
        self._scope_initialized = True
        self.scope_list.clear()
        # An explicit list refresh does not add a newly opened tab to the checked scope.
        for session in sessions:
            item = QListWidgetItem(
                tr(
                    "{p0} · Pane {p1} · {p2}",
                    p0=f"{session.title}",
                    p1=f"{session.pane_number}",
                    p2=tr(session.state.value, context="terminal-state"),
                ),
                self.scope_list,
            )
            item.setData(Qt.ItemDataRole.UserRole, session.session_id)
            eligible = session.state in (OpenTerminalState.OPEN, OpenTerminalState.ENDED)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if not eligible:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            selected = session.session_id in previous if initialized else session.active
            item.setCheckState(
                Qt.CheckState.Checked if selected and eligible else Qt.CheckState.Unchecked
            )
            item.setToolTip(item.text())

    @property
    def selected_session_ids(self) -> tuple[UUID, ...]:
        return tuple(
            self.scope_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.scope_list.count())
            if self.scope_list.item(index).checkState() == Qt.CheckState.Checked
        )

    @property
    def selected_hit(self) -> TerminalSearchHit | None:
        item = self.result_tree.currentItem()
        if item is None or not item.isSelected():
            return None
        index = item.data(0, Qt.ItemDataRole.UserRole)
        return self._hits[index] if type(index) is int and 0 <= index < len(self._hits) else None

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.search_button.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.scope_list.setEnabled(not busy)
        self.query_input.setEnabled(not busy)
        self.case_sensitive.setEnabled(not busy)
        self.whole_words.setEnabled(not busy)
        self._update_switch()
        if busy:
            self.show_status(tr("Searching frozen output…"))

    def clear_results(self) -> None:
        self._hits = ()
        self.result_tree.clear()
        self._update_switch()

    def _invalidate_results(self, *_args) -> None:
        self.clear_results()
        self.show_status(tr("Press Search to capture the checked scope with these options."))

    def set_result(
        self, result: TerminalMultiSearchResult, *, sources: int, skipped: int, budget_skipped: int
    ) -> None:
        self.clear_results()
        self._hits = result.hits
        for index, hit in enumerate(result.hits):
            item = QTreeWidgetItem(
                (
                    tr("{p0} · Pane {p1}", p0=f"{hit.title}", p1=f"{hit.pane_number}"),
                    str(hit.row_number),
                    hit.preview,
                )
            )
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            item.setToolTip(2, hit.preview)
            self.result_tree.addTopLevelItem(item)
        self.show_status(
            tr(
                "{p0} matching rows in {p1} captured terminals. Skipped unavailable: {p2}; over capture budget: {p3}. ",
                p0=f"{len(result.hits)}",
                p1=f"{sources}",
                p2=f"{skipped}",
                p3=f"{budget_skipped}",
            )
            + (tr("Result limit reached; search is incomplete. ") if result.truncated else "")
            + tr(
                "Output may have changed. Switch selects only the original terminal, not this snapshot row."
            )
        )

    def show_status(self, message: str) -> None:
        self.status_label.setText(message)

    def _update_switch(self) -> None:
        self.switch_button.setEnabled(not self._busy and self.selected_hit is not None)

    def _request_selected(self) -> None:
        if not self._busy and (hit := self.selected_hit) is not None:
            self.result_requested.emit(hit)
