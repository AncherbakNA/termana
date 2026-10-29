"""Native split panes which retain each terminal's widget and owning tab group."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from termana.i18n import tr
from termana.models.terminal_layout import (
    DEFAULT_PANE_SPLIT_RATIO,
    MAX_TERMINAL_PANES,
    PaneLeaf,
    PaneOrientation,
    PaneSplit,
    TerminalPaneLayout,
)
from termana.ui_typography import scale_widget_font
from termana.widgets.terminal_tab_bar import TerminalTabBar
from termana.widgets.terminal_tab_close import install_tab_close_button


@dataclass(frozen=True)
class _TabMetadata:
    """Tab presentation travels with the page rather than its old position."""

    text: str
    icon: QIcon
    tooltip: str
    whats_this: str
    enabled: bool
    visible: bool
    color: QColor
    data: object

    @classmethod
    def read(cls, tabs: QTabWidget, index: int) -> "_TabMetadata":
        return cls(
            tabs.tabText(index),
            tabs.tabIcon(index),
            tabs.tabToolTip(index),
            tabs.tabWhatsThis(index),
            tabs.isTabEnabled(index),
            tabs.isTabVisible(index),
            tabs.tabBar().tabTextColor(index),
            tabs.tabBar().tabData(index),
        )

    def insert(self, tabs: QTabWidget, index: int, page: QWidget, label: str | None = None) -> int:
        index = tabs.insertTab(index, page, self.icon, self.text if label is None else label)
        tabs.setTabToolTip(index, self.tooltip)
        tabs.setTabWhatsThis(index, self.whats_this)
        tabs.setTabEnabled(index, self.enabled)
        tabs.setTabVisible(index, self.visible)
        tabs.tabBar().setTabTextColor(index, self.color)
        tabs.tabBar().setTabData(index, self.data)
        return index


class _PaneTabs(QTabWidget):
    """Keep the empty surface in sync even when callers directly change tabs."""

    contents_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabBar(TerminalTabBar(self))

    def tabInserted(self, index: int) -> None:
        super().tabInserted(index)
        install_tab_close_button(self, index)
        self.contents_changed.emit()

    def setTabsClosable(self, closeable: bool) -> None:
        super().setTabsClosable(closeable)
        if closeable:
            for index in range(self.count()):
                install_tab_close_button(self, index)

    def tabRemoved(self, index: int) -> None:
        super().tabRemoved(index)
        self.contents_changed.emit()


class _PaneFrame(QFrame):
    """One independently selectable tab group and its empty-state surface."""

    def __init__(self, manager: "TerminalPaneManager") -> None:
        super().__init__(manager)
        self.setObjectName("terminalPane")
        self.setProperty("activePane", False)
        self.setProperty("multiplePanes", False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.header = QLabel(self)
        self.header.setObjectName("terminalPaneHeader")
        scale_widget_font(self.header, 0.9, weight=QFont.Weight.DemiBold)
        self.header.setContentsMargins(8, 3, 8, 3)
        self.header.hide()
        layout.addWidget(self.header)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)
        self.tabs = _PaneTabs(self.stack)
        self.tabs.setObjectName("terminalTabs")
        self.stack.addWidget(self.tabs)
        self.empty = QFrame(self.stack)
        self.empty.setObjectName("terminalPaneEmpty")
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setContentsMargins(16, 16, 16, 16)
        empty_layout.setSpacing(8)
        empty_layout.addStretch(1)
        title = QLabel(tr("Empty pane"), self.empty)
        title.setObjectName("terminalPaneEmptyTitle")
        scale_widget_font(title, 1.25, weight=QFont.Weight.DemiBold)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instruction = QLabel(tr("Create or open a saved connection."), self.empty)
        instruction.setObjectName("terminalPaneEmptyHint")
        instruction.setWordWrap(True)
        instruction.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.new_connection_button = QPushButton(tr("New connection"), self.empty)
        self.new_connection_button.setProperty("actionRole", "secondary")
        self.new_connection_button.setAccessibleName(
            tr("Create a new saved connection in this pane")
        )
        empty_layout.addWidget(title)
        empty_layout.addWidget(instruction)
        empty_layout.addWidget(self.new_connection_button, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch(1)
        self.stack.addWidget(self.empty)
        self.tabs.contents_changed.connect(self.sync_empty_state)
        for surface in (
            self,
            self.header,
            self.stack,
            self.tabs,
            self.tabs.tabBar(),
            self.empty,
            title,
            instruction,
            self.new_connection_button,
        ):
            surface.installEventFilter(manager)
        self.new_connection_button.clicked.connect(self._request_open_session)
        self.sync_empty_state()

    @Slot()
    def sync_empty_state(self) -> None:
        self.stack.setCurrentWidget(self.tabs if self.tabs.count() else self.empty)

    @Slot()
    def _request_open_session(self) -> None:
        manager = self._manager()
        if manager is not None and manager.set_active_tabs(self.tabs):
            manager.open_session_requested.emit()

    def _manager(self) -> "TerminalPaneManager | None":
        ancestor = self.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, TerminalPaneManager):
                return ancestor
            ancestor = ancestor.parentWidget()
        return None


class TerminalPaneManager(QFrame):
    """Manage bounded native, independently tabbed terminal panes."""

    pane_created = Signal(object)
    active_pane_changed = Signal(object)
    maximized_changed = Signal(bool)
    layout_changed = Signal()
    open_session_requested = Signal()
    tab_dropped = Signal()
    tab_drag_cancelled = Signal()

    MAX_PANES = MAX_TERMINAL_PANES

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("terminalPaneManager")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._panes: dict[QTabWidget, _PaneFrame] = {}
        # PySide's replaceWidget can leave a promoted splitter Python-owned;
        # keep its wrapper alive so collecting it cannot delete live descendants.
        self._splitters: set[QSplitter] = set()
        self._splitter_ratios: dict[QSplitter, int] = {}
        self._pending_splitter_sizes: set[QSplitter] = set()
        self._maximized_tabs: QTabWidget | None = None
        self._maximized_hidden_nodes: tuple[QWidget, ...] = ()
        self._maximized_ratios: dict[QSplitter, int] = {}
        self._ratio_timer = QTimer(self)
        self._ratio_timer.setSingleShot(True)
        self._ratio_timer.timeout.connect(self._apply_pending_splitter_sizes)
        self._transaction_depth = 0
        self._drag_page = None
        self._drag_source = None
        self._drop_target = None
        self.tab_move_guard = lambda: True
        initial = self._create_pane()
        self._root: QWidget = initial
        self._active_tabs: QTabWidget = initial.tabs
        self._layout.addWidget(initial)
        self._refresh_presentation()
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application.focusChanged.connect(self._handle_focus_changed)

    @property
    def active_tabs(self) -> QTabWidget:
        return self._active_tabs

    @property
    def tab_groups(self) -> tuple[QTabWidget, ...]:
        """Return real tab groups in splitter-tree visual order."""

        def walk(node: QWidget) -> Iterator[QTabWidget]:
            if isinstance(node, _PaneFrame):
                yield node.tabs
            elif isinstance(node, QSplitter):
                for index in range(node.count()):
                    yield from walk(node.widget(index))

        return tuple(walk(self._root))

    @property
    def pane_count(self) -> int:
        return len(self._panes)

    @property
    def is_changing(self) -> bool:
        """Let page show-event guards distinguish layout changes from user selection."""
        return bool(self._transaction_depth)

    @property
    def is_maximized(self) -> bool:
        """Whether the active pane temporarily fills the existing splitter tree."""
        return self._maximized_tabs is not None

    def toggle_maximize_active(self) -> bool:
        """Hide sibling branches without moving pages or changing the saved layout."""
        if self.is_maximized:
            return self.restore_maximized()
        if self.pane_count < 2 or self.active_tabs not in self._panes:
            return False
        # Capture every branch, including splitters outside the active path.
        # A hidden splitter's sizes (and a maximized ancestor's sizes) cannot
        # subsequently be used to infer the normal proportions.
        ratios = {splitter: self._splitter_ratio(splitter) for splitter in self._splitters}
        siblings: list[QWidget] = []
        node: QWidget = self._panes[self.active_tabs]
        while isinstance(parent := node.parentWidget(), QSplitter):
            siblings.append(parent.widget(1 if parent.indexOf(node) == 0 else 0))
            node = parent
        with self._changing(*self.tab_groups):
            self._maximized_tabs = self.active_tabs
            self._maximized_ratios = ratios
            self._maximized_hidden_nodes = tuple(siblings)
            self._ratio_timer.stop()
            for sibling in siblings:
                sibling.hide()
            self._layout.activate()
        self.maximized_changed.emit(True)
        self.layout_changed.emit()
        return True

    def restore_maximized(self) -> bool:
        """Reveal the normal tree, applying its saved proportions at its new size."""
        if not self.is_maximized:
            return False
        with self._changing(*self.tab_groups):
            siblings = self._maximized_hidden_nodes
            self._splitter_ratios.update(self._maximized_ratios)
            self._pending_splitter_sizes.update(self._splitters)
            self._maximized_tabs = None
            self._maximized_hidden_nodes = ()
            self._maximized_ratios = {}
            # Reveal outer branches first, so nested descendants have their
            # real parent geometry when Qt delivers their show/resize events.
            for sibling in reversed(siblings):
                sibling.show()
            self._apply_pending_splitter_sizes()
            self._schedule_splitter_sizes()
        self.maximized_changed.emit(False)
        self.layout_changed.emit()
        return True

    def capture_layout(self) -> TerminalPaneLayout:
        """Capture only ordered pane geometry, never tabs or terminal sessions."""

        def capture(node: QWidget) -> PaneLeaf | PaneSplit:
            if isinstance(node, _PaneFrame):
                return PaneLeaf()
            assert isinstance(node, QSplitter) and node.count() == 2
            orientation = (
                PaneOrientation.HORIZONTAL
                if node.orientation() == Qt.Orientation.Horizontal
                else PaneOrientation.VERTICAL
            )
            return PaneSplit(
                orientation,
                capture(node.widget(0)),
                capture(node.widget(1)),
                self._splitter_ratio(node),
            )

        return TerminalPaneLayout(capture(self._root), self.tab_groups.index(self.active_tabs))

    def restore_layout(self, layout: TerminalPaneLayout) -> bool:
        """Restore empty panes; a single live tab makes the operation a no-op."""
        if type(layout) is not TerminalPaneLayout:
            raise TypeError("layout must be TerminalPaneLayout")
        layout.__post_init__()
        groups = self.tab_groups
        if any(tabs.count() for tabs in groups):
            return False
        return self.apply_layout(layout, preserve_selection=False)

    def apply_layout(self, layout: TerminalPaneLayout, *, preserve_selection: bool = True) -> bool:
        """Rebuild only splitter geometry, retaining live groups and their pages.

        Surviving groups retain identity (including pending connection targets).
        Excess groups append their tabs to the last survivor before retirement.
        """
        if type(layout) is not TerminalPaneLayout:
            raise TypeError("layout must be TerminalPaneLayout")
        layout.__post_init__()
        groups = self.tab_groups
        previous_active = self.active_tabs
        selected = previous_active.currentWidget()
        created: list[QTabWidget] = []
        nodes: list[QWidget] = []
        with self._changing(*groups):
            self.restore_maximized()
            survivors = list(groups[: layout.pane_count])
            target = survivors[-1]
            for tabs in groups[layout.pane_count :]:
                while tabs.count():
                    self._transfer_page(tabs.widget(0), tabs, target)
                self._remove_pane(tabs)
            while len(survivors) < layout.pane_count:
                pane = self._create_pane()
                survivors.append(pane.tabs)
                created.append(pane.tabs)
            self._ratio_timer.stop()
            self._layout.removeWidget(self._root)
            # Detach every survivor before deleting old splitters: Qt owns
            # descendants, so deleting a populated tree would delete sessions.
            for tabs in survivors:
                pane = self._panes[tabs]
                pane.hide()
                pane.setParent(self)
            for splitter in self._splitters:
                splitter.hide()
                splitter.setParent(self)
                splitter.deleteLater()
            self._splitters.clear()
            self._splitter_ratios.clear()
            self._pending_splitter_sizes.clear()
            leaves = iter(survivors)

            def build(node: PaneLeaf | PaneSplit) -> QWidget:
                if isinstance(node, PaneLeaf):
                    pane = self._panes[next(leaves)]
                    nodes.append(pane)
                    return pane
                orientation = (
                    Qt.Orientation.Horizontal
                    if node.orientation == PaneOrientation.HORIZONTAL
                    else Qt.Orientation.Vertical
                )
                splitter = self._create_splitter(orientation, node.ratio)
                splitter.addWidget(build(node.first))
                splitter.addWidget(build(node.second))
                splitter.setCollapsible(0, False)
                splitter.setCollapsible(1, False)
                nodes.append(splitter)
                return splitter

            self._root = build(layout.root)
            self._layout.addWidget(self._root)
            if preserve_selection:
                self._active_tabs = previous_active if previous_active in survivors else target
                if selected is not None:
                    self._active_tabs.setCurrentWidget(selected)
            else:
                self._active_tabs = survivors[layout.active_pane]
            self._refresh_presentation()
            # Descendants first, root last: no half-built visible splitter tree.
            for node in nodes:
                node.show()
            self._layout.activate()
            # Record the intended focus even while hidden, so Qt's first-show
            # traversal cannot replace the restored selection with pane zero.
            self.focus_active_empty_pane()
            self._schedule_splitter_sizes()
            for tabs in created:
                self.pane_created.emit(tabs)
            if previous_active is not self.active_tabs:
                self.active_pane_changed.emit(self.active_tabs)
        self.layout_changed.emit()
        return True

    def focus_active_empty_pane(self) -> bool:
        """Route keyboard input away from the old terminal after selecting an empty pane."""
        if self.active_tabs.count():
            return False
        self._panes[self.active_tabs].new_connection_button.setFocus(
            Qt.FocusReason.OtherFocusReason
        )
        return True

    def owner_of(self, page: QWidget) -> QTabWidget | None:
        return next((tabs for tabs in self.tab_groups if tabs.indexOf(page) >= 0), None)

    def set_active_tabs(self, tabs: QTabWidget) -> bool:
        """Select a live pane, rejecting references to already merged panes."""
        if tabs not in self._panes:
            return False
        if self._active_tabs is not tabs:
            self.restore_maximized()
            self._active_tabs = tabs
            self._panes[tabs].sync_empty_state()
            self._refresh_presentation()
            self.active_pane_changed.emit(tabs)
        return True

    def split_active(self, orientation: Qt.Orientation) -> QTabWidget | None:
        """Split the active leaf without touching any existing session widget."""
        if orientation not in (Qt.Orientation.Horizontal, Qt.Orientation.Vertical):
            raise ValueError("orientation must be horizontal or vertical")
        if self.pane_count >= self.MAX_PANES:
            return None
        with self._changing(self.active_tabs):
            self.restore_maximized()
            original = self._panes[self.active_tabs]
            original_size = original.size()
            new = self._create_pane()
            splitter = self._create_splitter(orientation)
            splitter.resize(original_size)
            # replaceWidget can briefly give a visible child zero geometry.
            # Keep terminal views hidden until both new leaves have real sizes.
            original.hide()
            self._replace_node(original, splitter, show=False)
            self._layout.activate()
            splitter.resize(original_size)
            splitter.addWidget(original)
            splitter.addWidget(new)
            splitter.setCollapsible(0, False)
            splitter.setCollapsible(1, False)
            span = (
                splitter.width() if orientation == Qt.Orientation.Horizontal else splitter.height()
            )
            available = max(2, span - splitter.handleWidth())
            splitter.setSizes([available // 2, available - available // 2])
            original.show()
            new.show()
            splitter.show()
            self._schedule_splitter_sizes()
            self.pane_created.emit(new.tabs)
            self.set_active_tabs(new.tabs)
        self.layout_changed.emit()
        return new.tabs

    def move_page(self, page: QWidget, target: QTabWidget) -> bool:
        """Move the same page into another group, retaining its tab presentation."""
        source = self.owner_of(page)
        if source is None or target not in self._panes:
            return False
        with self._changing(source, target):
            if source is not target:
                self.restore_maximized()
            if source is not target:
                self._transfer_page(page, source, target)
            target.setCurrentWidget(page)
            self.set_active_tabs(target)
        self.layout_changed.emit()
        return True

    def merge_active(self) -> bool:
        """Append the active pane's tabs to the preceding pane (or next pane)."""
        groups = self.tab_groups
        if len(groups) == 1:
            return False
        source = self.active_tabs
        position = groups.index(source)
        target = groups[position - 1] if position else groups[1]
        selected = source.currentWidget()
        with self._changing(source, target):
            self.restore_maximized()
            while source.count():
                self._transfer_page(source.widget(0), source, target)
            if selected is not None:
                target.setCurrentWidget(selected)
            self._remove_pane(source)
            self.set_active_tabs(target)
        self.layout_changed.emit()
        return True

    def merge_all(self) -> bool:
        """Flatten panes into the first group, preserving visual order and selection."""
        groups = self.tab_groups
        if len(groups) == 1:
            return False
        selected = self.active_tabs.currentWidget()
        target = groups[0]
        with self._changing(*groups):
            self.restore_maximized()
            for source in groups[1:]:
                while source.count():
                    self._transfer_page(source.widget(0), source, target)
                self._remove_pane(source)
            if selected is not None:
                target.setCurrentWidget(selected)
            self.set_active_tabs(target)
            self._refresh_presentation()
        self.layout_changed.emit()
        return True

    def replace_page(self, old: QWidget, new: QWidget, label: str) -> bool:
        """Replace a page in place without deleting it or selecting a background pane."""
        tabs = self.owner_of(old)
        if tabs is None or old is new or self.owner_of(new) is not None:
            return False
        with self._changing(tabs):
            index = tabs.indexOf(old)
            selected = tabs.currentWidget()
            metadata = _TabMetadata.read(tabs, index)
            tabs.removeTab(index)
            old.hide()
            old.setParent(None)
            metadata.insert(tabs, index, new, label)
            if selected is not None:
                tabs.setCurrentWidget(new if selected is old else selected)
        self.layout_changed.emit()
        return True

    def _create_pane(self) -> _PaneFrame:
        pane = _PaneFrame(self)
        self._panes[pane.tabs] = pane
        pane.tabs.tabBarClicked.connect(self._handle_tab_clicked)
        pane.tabs.tabBar().cross_pane_drag_requested.connect(
            lambda page: self._start_tab_drag(page, pane.tabs)
        )
        pane.tabs.tabBar().drag_updated.connect(self._update_tab_drag)
        pane.tabs.tabBar().drag_finished.connect(self._finish_tab_drag)
        return pane

    def _start_tab_drag(self, page, tabs):
        if (
            not self.tab_move_guard()
            or self.owner_of(page) is not tabs
            or self.pane_count < 2
            or not hasattr(page, "session_id")
        ):
            return
        self._drag_page, self._drag_source = page, tabs.tabBar()

    def _drag_target_at(self, position):
        return next(
            (
                tabs
                for tabs, frame in self._panes.items()
                if frame.isVisible() and frame.rect().contains(frame.mapFromGlobal(position))
            ),
            None,
        )

    def _set_drop_target(self, target):
        if self._drop_target is target:
            return
        self._drop_target = target
        for tabs, frame in self._panes.items():
            if bool(frame.property("tabDropTarget")) != (tabs is target):
                frame.setProperty("tabDropTarget", tabs is target)
                frame.style().unpolish(frame)
                frame.style().polish(frame)
                frame.update()

    def _valid_tab_drag(self, page):
        return (
            page is not None
            and page is self._drag_page
            and isValid(page)
            and self.tab_move_guard()
            and self.owner_of(page) is not None
            and self.sender() is self._drag_source
        )

    def _update_tab_drag(self, page, position):
        self._set_drop_target(
            self._drag_target_at(position) if self._valid_tab_drag(page) else None
        )

    def _finish_tab_drag(self, page, position):
        target = self._drag_target_at(position) if self._valid_tab_drag(page) else None
        self._drag_page = self._drag_source = None
        self._set_drop_target(None)
        if target is None:
            self.tab_drag_cancelled.emit()
            return
        bar = target.tabBar()
        target_index = bar.tabAt(bar.mapFromGlobal(position)) if bar.isVisible() else -1
        if self.move_page(page, target):
            if target_index >= 0 and isValid(page) and self.owner_of(page) is target:
                bar.moveTab(target.indexOf(page), target_index)
            self.tab_dropped.emit()

    def _create_splitter(
        self, orientation: Qt.Orientation, ratio: int = DEFAULT_PANE_SPLIT_RATIO
    ) -> QSplitter:
        splitter = QSplitter(orientation, self)
        self._splitters.add(splitter)
        self._splitter_ratios[splitter] = ratio
        self._pending_splitter_sizes.add(splitter)
        splitter.setObjectName("terminalPaneSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        splitter.splitterMoved.connect(self._handle_splitter_moved)
        return splitter

    def _splitter_ratio(self, splitter: QSplitter) -> int:
        if self.is_maximized:
            return self._maximized_ratios[splitter]
        sizes = splitter.sizes()
        if (
            splitter in self._pending_splitter_sizes
            or not splitter.isVisible()
            or len(sizes) != 2
            or min(sizes) <= 0
        ):
            return self._splitter_ratios[splitter]
        ratio = max(1, min(9_999, round(sizes[0] * 10_000 / sum(sizes))))
        self._splitter_ratios[splitter] = ratio
        return ratio

    @Slot(int, int)
    def _handle_splitter_moved(self, _position: int, _index: int) -> None:
        splitter = self.sender()
        if (
            not self.is_maximized
            and isinstance(splitter, QSplitter)
            and splitter in self._splitters
        ):
            self._pending_splitter_sizes.discard(splitter)
            self._splitter_ratios[splitter] = self._splitter_ratio(splitter)
            self.layout_changed.emit()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._schedule_splitter_sizes()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_splitter_sizes()

    def _schedule_splitter_sizes(self) -> None:
        if not self.is_maximized and self._pending_splitter_sizes and self.isVisible():
            self._ratio_timer.start(0)

    @Slot()
    def _apply_pending_splitter_sizes(self) -> None:
        """Apply saved weights once after layout, parent before nested children.

        Native splitters handle subsequent resizing. Reapplying saved weights on
        every resize would fight a user's divider adjustments and minimum sizes.
        """
        if self.is_maximized or not self.isVisible() or not self._pending_splitter_sizes:
            return

        def apply(node: QWidget) -> None:
            if not isinstance(node, QSplitter):
                return
            if node in self._pending_splitter_sizes:
                sizes = node.sizes()
                if not node.isVisible() or len(sizes) != 2 or sum(sizes) <= 0:
                    return
                ratio = self._splitter_ratios[node]
                node.setSizes([ratio, 10_000 - ratio])
                self._pending_splitter_sizes.discard(node)
            for index in range(node.count()):
                apply(node.widget(index))

        # Geometry callbacks must not choose a pane or reopen an input prompt
        # while parent-before-child ratio restoration is still in progress.
        with self._changing(*self.tab_groups):
            self._layout.activate()
            apply(self._root)

    @Slot(int)
    def _handle_tab_clicked(self, _index: int) -> None:
        """Tab edits in a background pane must not steal the active session."""
        tabs = self.sender()
        if not self._transaction_depth and isinstance(tabs, QTabWidget):
            self.set_active_tabs(tabs)

    @Slot(QWidget, QWidget)
    def _handle_focus_changed(self, _old: QWidget | None, new: QWidget | None) -> None:
        if not self._transaction_depth:
            tabs = self._tabs_for_surface(new)
            if tabs is not None:
                self.set_active_tabs(tabs)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            not self._transaction_depth
            and event.type() == QEvent.Type.MouseButtonPress
            and isinstance(watched, QWidget)
        ):
            tabs = self._tabs_for_surface(watched)
            if tabs is not None:
                self.set_active_tabs(tabs)
        return super().eventFilter(watched, event)

    def _tabs_for_surface(self, surface: QWidget | None) -> QTabWidget | None:
        while surface is not None and surface is not self:
            if isinstance(surface, _PaneFrame):
                return surface.tabs if surface.tabs in self._panes else None
            surface = surface.parentWidget()
        return None

    @contextmanager
    def selection_transaction(self) -> Iterator[None]:
        """Suppress incidental selection changes while an input surface is replaced."""
        with self._changing(*self.tab_groups):
            yield

    @contextmanager
    def _changing(self, *groups: QTabWidget) -> Iterator[None]:
        """Publish only coherent final selections and ignore incidental focus changes."""
        before = {tabs: (tabs.currentIndex(), tabs.currentWidget()) for tabs in groups}
        focused = QApplication.focusWidget()
        # Retired panes may be deleted by nested event processing in a signal
        # receiver. A QSignalBlocker would retain a dangling native pointer.
        blocked_states = {tabs: tabs.blockSignals(True) for tabs in before}
        self._transaction_depth += 1
        try:
            yield
        finally:
            for tabs, was_blocked in blocked_states.items():
                if isValid(tabs):
                    tabs.blockSignals(was_blocked)
            try:
                for tabs in before:
                    if tabs in self._panes:
                        self._panes[tabs].sync_empty_state()
                current = self.active_tabs.currentWidget()
                if (
                    focused is not None
                    and isValid(focused)
                    and current is not None
                    and (focused is current or current.isAncestorOf(focused))
                ):
                    focused.setFocus(Qt.FocusReason.OtherFocusReason)
                for tabs, previous in before.items():
                    if tabs in self._panes and previous != (
                        tabs.currentIndex(),
                        tabs.currentWidget(),
                    ):
                        tabs.currentChanged.emit(tabs.currentIndex())
            finally:
                self._transaction_depth -= 1

    def _transfer_page(self, page: QWidget, source: QTabWidget, target: QTabWidget) -> None:
        index = source.indexOf(page)
        metadata = _TabMetadata.read(source, index)
        source.removeTab(index)
        metadata.insert(target, target.count(), page)

    def _replace_node(self, old: QWidget, new: QWidget, *, show: bool = True) -> None:
        parent = old.parentWidget()
        if isinstance(parent, QSplitter):
            index = parent.indexOf(old)
            sizes = parent.sizes()
            parent.replaceWidget(index, new)
            parent.setSizes(sizes)
        else:
            self._layout.removeWidget(old)
            old.setParent(None)
            self._layout.addWidget(new)
            self._root = new
        new.setVisible(show)

    def _remove_pane(self, tabs: QTabWidget) -> None:
        pane = self._panes.pop(tabs)
        splitter = pane.parentWidget()
        assert isinstance(splitter, QSplitter)
        sibling = splitter.widget(1 if splitter.indexOf(pane) == 0 else 0)
        sibling.setParent(None)
        self._replace_node(splitter, sibling)
        pane.hide()
        pane.setParent(None)
        pane.deleteLater()
        splitter.hide()
        splitter.setParent(None)
        splitter.deleteLater()
        self._splitters.discard(splitter)
        self._splitter_ratios.pop(splitter, None)
        self._pending_splitter_sizes.discard(splitter)

    def _refresh_presentation(self) -> None:
        multiple = self.pane_count > 1
        for number, tabs in enumerate(self.tab_groups, 1):
            tabs.tabBar().setProperty("crossPaneDragEnabled", multiple)
            pane = self._panes[tabs]
            pane.header.setText(tr("Pane {p0}", p0=f"{number}"))
            pane.header.setVisible(multiple)
            pane.setAccessibleName(tr("Terminal pane {p0}", p0=f"{number}"))
            active = tabs is self.active_tabs
            if pane.property("activePane") != active or pane.property("multiplePanes") != multiple:
                pane.setProperty("activePane", active)
                pane.setProperty("multiplePanes", multiple)
                pane.style().unpolish(pane)
                pane.style().polish(pane)
                pane.update()
