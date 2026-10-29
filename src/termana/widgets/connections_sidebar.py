"""Sidebar projection of the immutable saved-connections catalog."""

from typing import Final
from uuid import UUID

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QAccessible,
    QAccessibleEvent,
    QColor,
    QDropEvent,
    QFont,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOption,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from termana.connection_colors import connection_icon_color
from termana.i18n import tr
from termana.models.connection import (
    ConnectionCatalog,
    ConnectionFolder,
    ConnectionProfile,
    ConnectionProtocol,
)
from termana.qt_lifecycle import schedule_delete_later
from termana.session_filter import SessionFilterIndex, session_filter_terms
from termana.telnet_encoding_labels import telnet_encoding_label
from termana.theme import DEFAULT_THEME, ThemeColors
from termana.ui_icons import interface_icon, selection_interface_icon
from termana.ui_text import escape_qt_mnemonic, plain_text_tooltip
from termana.ui_toolbar import workbench_icon_colors
from termana.ui_typography import scale_widget_font
from termana.widgets.wrapping_label import WrappingLabel

DomainItem = ConnectionFolder | ConnectionProfile
DropPosition = QAbstractItemView.DropIndicatorPosition
MoveRequest = tuple[UUID, UUID | None, int]
SESSION_TREE_INDENTATION: Final = 12


def _domain_item_id(item: DomainItem) -> UUID:
    """Return the stable identity shared by catalog and tree projections."""
    if isinstance(item, ConnectionFolder):
        return item.folder_id
    return item.profile_id


def _calculate_drop_move_request(
    catalog: ConnectionCatalog,
    item_id: UUID,
    target_item_id: UUID | None,
    position: DropPosition,
) -> MoveRequest | None:
    """Translate one visual drop position into post-removal catalog coordinates.

    Qt reports a target in the tree that still contains the dragged item.  The
    catalog API, however, inserts into the destination *after* removing that
    item.  Calculating against the filtered sibling list avoids the classic
    off-by-one error when reordering within one folder.
    """
    item = catalog.folder_by_id(item_id) or catalog.profile_by_id(item_id)
    if item is None:
        return None

    destination_parent_id: UUID | None
    insertion_index: int
    if position == DropPosition.OnViewport:
        destination_parent_id = None
        insertion_index = _append_index_after_removal(catalog, item_id, None)
    elif position == DropPosition.OnItem:
        if target_item_id is None:
            destination_parent_id = None
        elif catalog.folder_by_id(target_item_id) is not None:
            destination_parent_id = target_item_id
        else:
            return None
        insertion_index = _append_index_after_removal(
            catalog,
            item_id,
            destination_parent_id,
        )
    elif position in {DropPosition.AboveItem, DropPosition.BelowItem}:
        if target_item_id is None or target_item_id == item_id:
            return None
        target = catalog.folder_by_id(target_item_id) or catalog.profile_by_id(target_item_id)
        if target is None:
            return None
        destination_parent_id = catalog.parent_id_of(target_item_id)
        destination_ids = _child_ids_after_removal(
            catalog,
            item_id,
            destination_parent_id,
        )
        try:
            target_index = destination_ids.index(target_item_id)
        except ValueError:
            return None
        insertion_index = target_index + (position == DropPosition.BelowItem)
    else:
        return None

    try:
        candidate = catalog.with_item_moved(
            item_id,
            parent_folder_id=destination_parent_id,
            index=insertion_index,
        )
    except ValueError:
        return None
    if candidate == catalog:
        return None
    return item_id, destination_parent_id, insertion_index


def _child_ids_after_removal(
    catalog: ConnectionCatalog,
    item_id: UUID,
    parent_folder_id: UUID | None,
) -> tuple[UUID, ...]:
    """Return destination siblings in the coordinate system used by a move."""
    return tuple(
        _domain_item_id(child)
        for child in catalog.children_of(parent_folder_id)
        if _domain_item_id(child) != item_id
    )


def _append_index_after_removal(
    catalog: ConnectionCatalog,
    item_id: UUID,
    parent_folder_id: UUID | None,
) -> int:
    """Return an explicit append index after removing the moving item."""
    return len(_child_ids_after_removal(catalog, item_id, parent_folder_id))


class _SessionsTree(QTreeWidget):
    """Turn deterministic keyboard gestures into semantic requests."""

    activate_pressed = Signal()
    edit_pressed = Signal()
    delete_pressed = Signal()
    move_up_pressed = Signal()
    move_down_pressed = Signal()
    move_drop_requested = Signal(object, object, object)

    def __init__(self) -> None:
        """Track one stable domain identity for the lifetime of an internal drag."""
        super().__init__()
        self._dragged_item_id: UUID | None = None
        self._reordering_enabled = True
        self._branch_colors = DEFAULT_THEME

    def set_branch_colors(self, colors: ThemeColors) -> None:
        self._branch_colors = colors
        self.viewport().update()

    def drawBranches(self, painter, rect, index) -> None:
        """Paint one continuous row, not Qt's separate styled indentation frame.

        Qt still owns expansion, branch hit testing, navigation and the item cell.
        Only indentation paint is replaced; native arrow primitives stay native.
        """
        colors = self._branch_colors
        background = colors.raised if self.selectionModel().isSelected(index) else colors.surface
        painter.save()
        try:
            painter.setClipRect(rect)
            painter.fillRect(rect, QColor(background))
            if not self.model().hasChildren(index):
                return
            extent = min(10, max(1, self.indentation() - 2))
            left = (
                rect.right() - self.indentation() + 1
                if self.layoutDirection() == Qt.LayoutDirection.LeftToRight
                else rect.left()
            )
            option = QStyleOption()
            option.initFrom(self)
            option.rect = QRect(
                left + (self.indentation() - extent) // 2,
                rect.center().y() - extent // 2,
                extent,
                extent,
            )
            primitive = (
                QStyle.PrimitiveElement.PE_IndicatorArrowDown
                if self.isExpanded(index)
                else QStyle.PrimitiveElement.PE_IndicatorArrowRight
            )
            self.style().drawPrimitive(primitive, option, painter, self)
        finally:
            painter.restore()

    def set_reordering_enabled(self, enabled: bool) -> None:
        """Enable native moves only when the tree shows the complete catalog."""
        self._reordering_enabled = enabled
        self.setDragEnabled(enabled)
        self.setAcceptDrops(enabled)
        self.setDropIndicatorShown(enabled)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle domain items once and leave synthetic rows inert."""
        current_item = self.currentItem()
        domain_item = (
            current_item.data(0, Qt.ItemDataRole.UserRole) if current_item is not None else None
        )
        if not isinstance(domain_item, (ConnectionFolder, ConnectionProfile)):
            super().keyPressEvent(event)
            return

        requested_signal = None
        if (
            self._reordering_enabled
            and event.modifiers() == Qt.KeyboardModifier.AltModifier
            and event.key() == Qt.Key.Key_Up
        ):
            requested_signal = self.move_up_pressed
        elif (
            self._reordering_enabled
            and event.modifiers() == Qt.KeyboardModifier.AltModifier
            and event.key() == Qt.Key.Key_Down
        ):
            requested_signal = self.move_down_pressed
        elif event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            requested_signal = self.activate_pressed
        elif event.key() == Qt.Key.Key_F2:
            requested_signal = self.edit_pressed
        elif event.key() == Qt.Key.Key_Delete:
            requested_signal = self.delete_pressed

        if requested_signal is None:
            super().keyPressEvent(event)
            return
        if not event.isAutoRepeat():
            requested_signal.emit()
        event.accept()

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        """Begin only a single-item internal move and retain its stable UUID."""
        if not self._reordering_enabled:
            return
        item_id = self._selected_drag_item_id()
        if item_id is None or not supported_actions & Qt.DropAction.MoveAction:
            return
        self._dragged_item_id = item_id
        try:
            super().startDrag(Qt.DropAction.MoveAction)
        finally:
            self._dragged_item_id = None

    def _selected_drag_item_id(self) -> UUID | None:
        """Resolve exactly one selected domain row and reject synthetic/multi-selection."""
        selected_items = self.selectedItems()
        if len(selected_items) != 1:
            return None
        domain_item = selected_items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(domain_item, (ConnectionFolder, ConnectionProfile)):
            return None
        return _domain_item_id(domain_item)

    def dropEvent(self, event: QDropEvent) -> None:
        """Request a catalog move without mutating Qt's item model optimistically."""
        if (
            not self._reordering_enabled
            or event.source() is not self
            or event.proposedAction() != Qt.DropAction.MoveAction
            or self._dragged_item_id is None
            or self._selected_drag_item_id() != self._dragged_item_id
        ):
            event.ignore()
            return

        target_item = self.itemAt(event.position().toPoint())
        target = target_item.data(0, Qt.ItemDataRole.UserRole) if target_item is not None else None
        target_item_id = (
            _domain_item_id(target)
            if isinstance(target, (ConnectionFolder, ConnectionProfile))
            else None
        )
        self.move_drop_requested.emit(
            self._dragged_item_id,
            target_item_id,
            self.dropIndicatorPosition(),
        )
        # Ignoring the native event is deliberate: accepting MoveAction can make
        # QAbstractItemView remove the source row after startDrag returns.  The
        # persisted catalog snapshot will rebuild the projection asynchronously.
        event.ignore()


class ConnectionsSidebar(QFrame):
    """Display a catalog without deciding how or when it is persisted."""

    new_connection_requested = Signal(object)
    new_folder_requested = Signal(object)
    connection_activated = Signal(object)
    edit_connection_requested = Signal(object)
    duplicate_connection_requested = Signal(object)
    favorite_connection_requested = Signal(object, bool)
    export_item_requested = Signal(object)
    delete_connection_requested = Signal(object)
    rename_folder_requested = Signal(object)
    delete_folder_requested = Signal(object)
    move_item_requested = Signal(object, object, object)
    retry_catalog_requested = Signal()
    _queued_drop_requested = Signal(object, object, object, object, object)

    def __init__(self) -> None:
        """Build the sidebar widgets and their local interaction paths."""
        super().__init__()

        self.setObjectName("connectionsSidebar")
        self.setAccessibleName(tr("Connections"))
        # The outer rail owns the sidebar minimum; this page must fit the
        # remaining viewport even with a large UI font and a visible scrollbar.
        self.setMinimumWidth(0)

        shell_layout = QVBoxLayout(self)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        sidebar_scroll = QScrollArea()
        sidebar_scroll.setObjectName("connectionsSidebarScroll")
        sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_scroll.viewport().setObjectName("connectionsSidebarScrollViewport")

        sidebar_content = QWidget()
        sidebar_content.setObjectName("connectionsSidebarContent")
        layout = QVBoxLayout(sidebar_content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        sidebar_scroll.setWidget(sidebar_content)
        shell_layout.addWidget(sidebar_scroll)

        self.catalog_state_panel = QFrame()
        self.catalog_state_panel.setObjectName("connectionCatalogStatePanel")
        self.catalog_state_panel.setAccessibleName(tr("Saved connections unavailable"))
        catalog_state_layout = QVBoxLayout(self.catalog_state_panel)
        catalog_state_layout.setContentsMargins(14, 14, 14, 14)
        catalog_state_layout.setSpacing(8)

        catalog_state_title = WrappingLabel(tr("Saved connections unavailable"))
        catalog_state_title.setObjectName("connectionCatalogStateTitle")
        catalog_state_title.setTextFormat(Qt.TextFormat.PlainText)
        catalog_state_title.setWordWrap(True)
        scale_widget_font(catalog_state_title, 1.08, weight=QFont.Weight.DemiBold)

        catalog_state_description = WrappingLabel(
            tr("Retry after checking the saved connections file. Local terminals remain available.")
        )
        catalog_state_description.setObjectName("connectionCatalogStateDescription")
        catalog_state_description.setTextFormat(Qt.TextFormat.PlainText)
        catalog_state_description.setWordWrap(True)

        self.retry_catalog_button = QPushButton(tr("Retry"))
        self.retry_catalog_button.setObjectName("retryConnectionCatalogButton")
        self.retry_catalog_button.setAccessibleName(tr("Retry loading saved connections"))
        self.retry_catalog_button.setToolTip(tr("Try to load saved connections again"))

        catalog_state_layout.addWidget(catalog_state_title)
        catalog_state_layout.addWidget(catalog_state_description)
        catalog_state_layout.addWidget(self.retry_catalog_button)
        self.catalog_state_panel.hide()

        self.catalog_state_spacer = QWidget()
        self.catalog_state_spacer.setObjectName("connectionCatalogStateSpacer")
        self.catalog_state_spacer.setSizePolicy(
            QSizePolicy.Policy.Minimum,
            QSizePolicy.Policy.Expanding,
        )
        self.catalog_state_spacer.hide()

        self.sessions_filter_input = QLineEdit()
        self.sessions_filter_input.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.sessions_filter_input.setObjectName("sessionsFilterInput")
        self.sessions_filter_input.setAccessibleName(tr("Filter saved sessions"))
        self.sessions_filter_input.setAccessibleDescription(
            tr("Search saved folders and connections. All entered terms must match.")
        )
        self.sessions_filter_input.setPlaceholderText(tr("Search sessions…"))
        self.sessions_filter_input.setClearButtonEnabled(True)
        self.sessions_filter_input.setMaxLength(512)
        self.sessions_filter_input.setToolTip(
            tr("Search names, folder paths, protocols, hosts, and ports")
        )

        self.favorites_button = QPushButton(tr("Favorites"))
        self.favorites_button.setObjectName("favoritesFilterButton")
        self.favorites_button.setCheckable(True)
        self.favorites_button.setAccessibleName(tr("Show favorite connections only"))
        self.favorites_button.setAccessibleDescription(
            tr(
                "Show only favorite connections and their parent folders. The saved-session search also applies while this filter is enabled."
            )
        )
        self.favorites_button.setToolTip(tr("Show only favorite connections"))

        self.sessions_filter_status = WrappingLabel()
        self.sessions_filter_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.sessions_filter_status.setObjectName("sessionsFilterStatus")
        self.sessions_filter_status.setTextFormat(Qt.TextFormat.PlainText)
        self.sessions_filter_status.setProperty("filterState", "inactive")
        self.sessions_filter_status.hide()

        self.sessions_tree = _SessionsTree()
        self.sessions_tree.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.sessions_tree.setObjectName("sessionsTree")
        self.sessions_tree.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.sessions_tree.setAccessibleName(tr("Saved connections"))
        self.sessions_tree.setAccessibleDescription(
            tr(
                "Use Enter to open a connection or expand a folder, F2 to edit, Delete to request removal, and Alt or Option plus Up or Down to reorder when search and Favorites filters are off. The context menu includes additional profile actions such as Duplicate and Add to favorites, and Export connection or Export folder for the complete saved item."
            )
        )
        self.sessions_tree.setHeaderHidden(True)
        self.sessions_tree.setIndentation(SESSION_TREE_INDENTATION)
        self.sessions_tree.setIconSize(QSize(18, 18))
        self.sessions_tree.setUniformRowHeights(True)
        self.sessions_tree.setAllColumnsShowFocus(True)
        self.sessions_tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.sessions_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._connection_item_delegate = QStyledItemDelegate(self.sessions_tree)
        self.sessions_tree.setItemDelegate(self._connection_item_delegate)
        self.sessions_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sessions_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sessions_tree.setDragEnabled(True)
        self.sessions_tree.setAcceptDrops(True)
        self.sessions_tree.setDropIndicatorShown(True)
        self.sessions_tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.sessions_tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.sessions_tree.setDragDropOverwriteMode(False)

        self._sessions_root = QTreeWidgetItem([tr("User sessions")])
        self._sessions_root.setFlags(
            self._sessions_root.flags()
            & ~Qt.ItemFlag.ItemIsDragEnabled
            & ~Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        self._sessions_root.setData(
            0,
            Qt.ItemDataRole.AccessibleDescriptionRole,
            tr("Root of saved connection folders and profiles."),
        )
        self._empty_item: QTreeWidgetItem | None = None
        self._no_matches_item: QTreeWidgetItem | None = None
        self._items_by_folder_id: dict[UUID, QTreeWidgetItem] = {}
        self._items_by_profile_id: dict[UUID, QTreeWidgetItem] = {}
        self._interface_icons: dict[str, QIcon] = {}
        self._catalog = ConnectionCatalog()
        self._filter_index = SessionFilterIndex.from_catalog(self._catalog)
        self._catalog_generation = 0
        self._filter_terms: tuple[str, ...] = ()
        self._favorites_only = False
        self._pre_filter_expanded_folder_ids: set[UUID] | None = None
        self._pre_filter_root_expanded: bool | None = None
        self._pre_filter_selection_id: UUID | None = None
        self.sessions_tree.addTopLevelItem(self._sessions_root)
        self._sessions_root.setExpanded(True)
        self._show_empty_state()

        layout.addWidget(self.sessions_filter_input)
        layout.addWidget(self.catalog_state_panel)
        layout.addWidget(self.catalog_state_spacer, 1)
        layout.addWidget(self.favorites_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.sessions_filter_status)
        layout.addWidget(self.sessions_tree, 1)

        self.retry_catalog_button.clicked.connect(self._request_catalog_retry)
        self.sessions_filter_input.textChanged.connect(self._apply_sessions_filter)
        self.favorites_button.toggled.connect(self._apply_favorites_filter)
        self.sessions_tree.itemActivated.connect(self._activate_item)
        self.sessions_tree.itemExpanded.connect(self._refresh_folder_icon)
        self.sessions_tree.itemCollapsed.connect(self._refresh_folder_icon)
        self.sessions_tree.activate_pressed.connect(self._activate_current_item)
        self.sessions_tree.edit_pressed.connect(self._edit_current_item)
        self.sessions_tree.delete_pressed.connect(self._delete_current_item)
        self.sessions_tree.move_up_pressed.connect(self._move_current_item_up)
        self.sessions_tree.move_down_pressed.connect(self._move_current_item_down)
        self.sessions_tree.move_drop_requested.connect(self._queue_dropped_move)
        self._queued_drop_requested.connect(
            self._deliver_dropped_move,
            Qt.ConnectionType.QueuedConnection,
        )
        self.sessions_tree.customContextMenuRequested.connect(self._show_connection_context_menu)

        self.find_sessions_shortcut = QShortcut(
            QKeySequence(QKeySequence.StandardKey.Find),
            self,
        )
        self.find_sessions_shortcut.setObjectName("findSavedSessionsShortcut")
        self.find_sessions_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.find_sessions_shortcut.setAutoRepeat(False)
        self.find_sessions_shortcut.activated.connect(self._focus_sessions_filter)

        self.clear_sessions_filter_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Escape),
            self.sessions_filter_input,
        )
        self.clear_sessions_filter_shortcut.setObjectName("clearSavedSessionsFilterShortcut")
        self.clear_sessions_filter_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.clear_sessions_filter_shortcut.setAutoRepeat(False)
        self.clear_sessions_filter_shortcut.activated.connect(self._clear_sessions_filter)
        self.set_interface_colors(DEFAULT_THEME)

    def set_interface_colors(self, colors: ThemeColors, *, monochrome: bool = False) -> None:
        """Cache a handful of tree glyphs; recolor rows without rebuilding catalog state."""
        self.sessions_tree.set_branch_colors(colors)
        self._connection_icon_background = colors.surface
        self._connection_selected_background = colors.raised
        self._connection_color_icons = {}
        tones = workbench_icon_colors(colors, monochrome=monochrome)
        self._interface_icons = {
            "folder": interface_icon("folder", tones["folder"]),
            "folder_open": interface_icon("folder_open", tones["folder"]),
            "ssh": interface_icon("server", colors.text),
            "telnet": interface_icon("terminal", tones["terminal"]),
            "rdp": interface_icon("desktop", colors.text),
        }
        self._refresh_folder_icon(self._sessions_root)
        for item in self._items_by_folder_id.values():
            self._refresh_folder_icon(item)
        for item in self._items_by_profile_id.values():
            profile = item.data(0, Qt.ItemDataRole.UserRole)
            item.setIcon(0, self._profile_icon(profile))

    def _profile_icon(self, profile: ConnectionProfile) -> QIcon:
        if profile.accent_color is None:
            return self._interface_icons[profile.protocol.value]
        glyph = {ConnectionProtocol.SSH: "server", ConnectionProtocol.TELNET: "terminal",
                 ConnectionProtocol.RDP: "desktop"}[profile.protocol]
        key = (profile.protocol, profile.accent_color)
        icon = self._connection_color_icons.get(key)
        if icon is None:
            tone = connection_icon_color(profile.accent_color, self._connection_icon_background,
                                         self._connection_selected_background)
            icon = selection_interface_icon(glyph, tone, tone)
            if len(self._connection_color_icons) >= 128:
                self._connection_color_icons.clear()
            self._connection_color_icons[key] = icon
        return icon

    def _refresh_folder_icon(self, item: QTreeWidgetItem) -> None:
        """Reflect expansion for the root and typed folders, never profile rows."""
        if not self._interface_icons:
            return
        if item is self._sessions_root or isinstance(
            item.data(0, Qt.ItemDataRole.UserRole), ConnectionFolder
        ):
            item.setIcon(0, self._interface_icons["folder_open" if item.isExpanded() else "folder"])

    def set_catalog_available(self, available: bool) -> None:
        """Switch between the saved catalog and one honest recoverable error state."""
        if type(available) is not bool:
            raise TypeError("available must be a bool")

        catalog_widgets = (
            self.sessions_filter_input,
            self.favorites_button,
            self.sessions_filter_status,
            self.sessions_tree,
        )
        if not available:
            self.sessions_filter_input.clear()
            self.favorites_button.setChecked(False)
            for widget in catalog_widgets:
                widget.hide()
                widget.setEnabled(False)
            self.catalog_state_panel.show()
            self.catalog_state_spacer.show()
            self.retry_catalog_button.setFocus()
            QAccessible.updateAccessibility(
                QAccessibleEvent(self.catalog_state_panel, QAccessible.Event.Alert)
            )
            return

        self.catalog_state_panel.hide()
        self.catalog_state_spacer.hide()
        for widget in catalog_widgets:
            widget.setEnabled(True)
        self.sessions_filter_input.show()
        self.favorites_button.show()
        self.sessions_tree.show()
        self._update_filter_status(
            len(
                self._filter_index.project(
                    self._filter_terms, favorites_only=self._favorites_only
                ).matching_item_ids
            )
        )

    def _request_catalog_retry(self, _checked: bool = False) -> None:
        """Request a fresh load without coupling the view to disk persistence."""
        self.retry_catalog_requested.emit()

    @property
    def catalog(self) -> ConnectionCatalog:
        """Return the immutable snapshot currently projected by the tree."""
        return self._catalog

    def catalog_item_is_visible(self, item_id: UUID) -> bool:
        """Return whether one current catalog row is visible under the active filter."""
        if not isinstance(item_id, UUID):
            raise TypeError("item_id must be a UUID")
        item = self._item_by_id(item_id)
        return item is not None and not item.isHidden()

    def set_catalog(
        self,
        catalog: ConnectionCatalog,
        *,
        select_item_id: UUID | None = None,
    ) -> None:
        """Rebuild from one snapshot while retaining useful UI-only state."""
        if not isinstance(catalog, ConnectionCatalog):
            raise TypeError("catalog must be ConnectionCatalog")
        if select_item_id is not None and (
            catalog.folder_by_id(select_item_id) is None
            and catalog.profile_by_id(select_item_id) is None
        ):
            raise ValueError("Selected connection catalog item does not exist")

        current_domain = self._current_domain_item()
        previous_selection_id = (
            _domain_item_id(current_domain) if current_domain is not None else None
        )
        selected_id = select_item_id if select_item_id is not None else previous_selection_id

        if self._filter_is_active():
            existing_folder_ids = {folder.folder_id for folder in catalog.folders}
            if self._pre_filter_expanded_folder_ids is not None:
                self._pre_filter_expanded_folder_ids.intersection_update(existing_folder_ids)
            if select_item_id is not None:
                self._pre_filter_selection_id = select_item_id
                self._pre_filter_root_expanded = True
                if self._pre_filter_expanded_folder_ids is not None:
                    self._pre_filter_expanded_folder_ids.update(
                        self._ancestor_folder_ids(catalog, select_item_id)
                    )
            elif self._pre_filter_selection_id is not None and (
                catalog.folder_by_id(self._pre_filter_selection_id) is None
                and catalog.profile_by_id(self._pre_filter_selection_id) is None
            ):
                self._pre_filter_selection_id = None
            self._rebuild_catalog_projection(
                catalog,
                selected_item_id=selected_id,
                fallback_selected_item_id=previous_selection_id,
            )
            return

        expanded_folder_ids = self._expanded_folder_ids()
        root_expanded = self._sessions_root.isExpanded()
        self._rebuild_catalog_projection(
            catalog,
            selected_item_id=selected_id,
            expanded_folder_ids=expanded_folder_ids,
            root_expanded=root_expanded,
            reveal_selection=select_item_id is not None,
        )

    def _rebuild_catalog_projection(
        self,
        catalog: ConnectionCatalog,
        *,
        selected_item_id: UUID | None,
        fallback_selected_item_id: UUID | None = None,
        expanded_folder_ids: set[UUID] | None = None,
        root_expanded: bool = True,
        reveal_selection: bool = False,
    ) -> None:
        """Rebuild the complete persistent tree for one new catalog snapshot."""
        self.sessions_tree.setUpdatesEnabled(False)
        try:
            self._catalog_generation += 1
            self._sessions_root.takeChildren()
            self._empty_item = None
            self._no_matches_item = None
            self._items_by_folder_id.clear()
            self._items_by_profile_id.clear()
            self._catalog = catalog
            self._filter_index = SessionFilterIndex.from_catalog(catalog)

            for domain_item in catalog.children_of(None):
                self._append_domain_item(self._sessions_root, domain_item)

            if not catalog.root_item_ids:
                self._show_empty_state()

            for folder_id in expanded_folder_ids or ():
                folder_item = self._items_by_folder_id.get(folder_id)
                if folder_item is not None:
                    folder_item.setExpanded(True)
            self._sessions_root.setExpanded(root_expanded)

            if self._filter_is_active():
                self._apply_filter_to_persistent_tree(
                    selected_item_id=selected_item_id,
                    fallback_selected_item_id=fallback_selected_item_id,
                )
            else:
                selected_item = (
                    self._item_by_id(selected_item_id) if selected_item_id is not None else None
                )
                if selected_item is not None:
                    if reveal_selection:
                        self._expand_ancestors(selected_item_id)
                        self._sessions_root.setExpanded(True)
                    self.sessions_tree.setCurrentItem(selected_item)
                else:
                    self.sessions_tree.setCurrentItem(self._sessions_root)
                self._update_filter_status(0)
                self.sessions_tree.set_reordering_enabled(True)
        finally:
            self.sessions_tree.setUpdatesEnabled(True)
            self.sessions_tree.viewport().update()

    def _apply_filter_to_persistent_tree(
        self,
        *,
        selected_item_id: UUID | None,
        fallback_selected_item_id: UUID | None = None,
    ) -> None:
        """Hide or reveal existing rows without recreating any domain tree item."""
        filter_projection = self._filter_index.project(
            self._filter_terms, favorites_only=self._favorites_only
        )
        visible_item_ids = filter_projection.visible_item_ids
        for item_id, tree_item in self._items_by_folder_id.items():
            tree_item.setHidden(item_id not in visible_item_ids)
        for item_id, tree_item in self._items_by_profile_id.items():
            tree_item.setHidden(item_id not in visible_item_ids)

        if self._filter_is_active():
            if self._catalog.root_item_ids and not visible_item_ids:
                self._show_no_matches_state()
            else:
                self._remove_no_matches_state()
            for folder_id in visible_item_ids:
                folder_item = self._items_by_folder_id.get(folder_id)
                if folder_item is not None:
                    folder_item.setExpanded(True)
            self._sessions_root.setExpanded(True)
            selected_item = (
                self._item_by_id(selected_item_id) if selected_item_id is not None else None
            )
            if selected_item is not None and not selected_item.isHidden():
                self.sessions_tree.setCurrentItem(selected_item)
            else:
                fallback_item = (
                    self._item_by_id(fallback_selected_item_id)
                    if fallback_selected_item_id is not None
                    else None
                )
                self.sessions_tree.setCurrentItem(
                    fallback_item
                    if fallback_item is not None and not fallback_item.isHidden()
                    else self._sessions_root
                )
        else:
            self._remove_no_matches_state()

        self._update_filter_status(len(filter_projection.matching_item_ids))
        self.sessions_tree.set_reordering_enabled(not self._filter_is_active())

    def _apply_sessions_filter(self, value: str) -> None:
        """Combine the current search query with the independent Favorites toggle."""
        self._set_filters(session_filter_terms(value), self._favorites_only)

    def _apply_favorites_filter(self, checked: bool) -> None:
        """Keep query and persistent row identities intact when toggling Favorites."""
        self._set_filters(self._filter_terms, checked)

    def _filter_is_active(self) -> bool:
        """Treat either search or Favorites as a partial catalog projection."""
        return bool(self._filter_terms) or self._favorites_only

    def _set_filters(self, terms: tuple[str, ...], favorites_only: bool) -> None:
        """Update row visibility while keeping the complete catalog immutable."""
        was_active = self._filter_is_active()
        is_active = bool(terms) or favorites_only
        if terms == self._filter_terms and favorites_only == self._favorites_only:
            return

        if not was_active and is_active:
            self._pre_filter_expanded_folder_ids = self._expanded_folder_ids()
            self._pre_filter_root_expanded = self._sessions_root.isExpanded()
            current_domain = self._current_domain_item()
            self._pre_filter_selection_id = (
                _domain_item_id(current_domain) if current_domain is not None else None
            )

        current_domain = self._current_domain_item()
        filtered_selection_id = (
            _domain_item_id(current_domain) if current_domain is not None else None
        )
        self._filter_terms = terms
        self._favorites_only = favorites_only
        self._catalog_generation += 1

        if was_active and not is_active:
            expanded_folder_ids = self._pre_filter_expanded_folder_ids or set()
            selected_item_id = self._pre_filter_selection_id
            self.sessions_tree.setUpdatesEnabled(False)
            try:
                self._apply_filter_to_persistent_tree(selected_item_id=None)
                for folder_id, folder_item in self._items_by_folder_id.items():
                    folder_item.setExpanded(folder_id in expanded_folder_ids)
                self._sessions_root.setExpanded(bool(self._pre_filter_root_expanded))
                selected_item = (
                    self._item_by_id(selected_item_id) if selected_item_id is not None else None
                )
                self.sessions_tree.setCurrentItem(selected_item or self._sessions_root)
            finally:
                self.sessions_tree.setUpdatesEnabled(True)
                self.sessions_tree.viewport().update()
            self._pre_filter_expanded_folder_ids = None
            self._pre_filter_root_expanded = None
            self._pre_filter_selection_id = None
            return

        self.sessions_tree.setUpdatesEnabled(False)
        try:
            self._apply_filter_to_persistent_tree(
                selected_item_id=filtered_selection_id,
            )
        finally:
            self.sessions_tree.setUpdatesEnabled(True)
            self.sessions_tree.viewport().update()

    def _update_filter_status(self, matching_item_count: int) -> None:
        """Expose an unambiguous plain-text summary for sighted and AT users."""
        if not self._filter_is_active() or not self._catalog.root_item_ids:
            previous_state = self.sessions_filter_status.property("filterState")
            self.sessions_filter_status.setProperty("filterState", "inactive")
            self.sessions_filter_status.clear()
            self.sessions_filter_status.setAccessibleName("")
            self.sessions_filter_status.hide()
            if previous_state != "inactive":
                self.sessions_filter_status.style().unpolish(self.sessions_filter_status)
                self.sessions_filter_status.style().polish(self.sessions_filter_status)
            return

        if matching_item_count == 0:
            message = (
                tr("No matching favorites")
                if self._favorites_only and self._filter_terms
                else tr("No favorite connections")
                if self._favorites_only
                else tr("No matching saved sessions")
            )
            filter_state = "noResults"
        elif self._favorites_only:
            noun = "favorite" if matching_item_count == 1 else "favorites"
            message = f"{matching_item_count} {noun}"
            filter_state = "results"
        elif matching_item_count == 1:
            message = tr("1 matching item")
            filter_state = "results"
        else:
            message = tr("{p0} matching items", p0=f"{matching_item_count}")
            filter_state = "results"
        previous_state = self.sessions_filter_status.property("filterState")
        previous_message = self.sessions_filter_status.text()
        self.sessions_filter_status.setProperty("filterState", filter_state)
        self.sessions_filter_status.setText(message)
        self.sessions_filter_status.setAccessibleName(message)
        self.sessions_filter_status.show()
        if previous_state != filter_state:
            self.sessions_filter_status.style().unpolish(self.sessions_filter_status)
            self.sessions_filter_status.style().polish(self.sessions_filter_status)
        if previous_message != message:
            event_type = (
                QAccessible.Event.Alert
                if filter_state == "noResults" and previous_state != "noResults"
                else QAccessible.Event.NameChanged
            )
            QAccessible.updateAccessibility(
                QAccessibleEvent(
                    self.sessions_filter_status,
                    event_type,
                )
            )

    def _focus_sessions_filter(self) -> None:
        """Focus and select the saved-session query for StandardKey.Find."""
        self.sessions_filter_input.setFocus()
        self.sessions_filter_input.selectAll()

    def _clear_sessions_filter(self) -> None:
        """Clear the query, retain Favorites, and return focus to the session tree."""
        self.sessions_filter_input.clear()
        self.sessions_tree.setFocus()

    def _expanded_folder_ids(self) -> set[UUID]:
        """Snapshot exact folder expansion without retaining Qt item objects."""
        return {
            folder_id for folder_id, item in self._items_by_folder_id.items() if item.isExpanded()
        }

    @staticmethod
    def _ancestor_folder_ids(catalog: ConnectionCatalog, item_id: UUID) -> set[UUID]:
        """Return the stable ancestor chain for one existing catalog item."""
        ancestor_ids: set[UUID] = set()
        parent_id = catalog.parent_id_of(item_id)
        while parent_id is not None:
            ancestor_ids.add(parent_id)
            parent_id = catalog.parent_id_of(parent_id)
        return ancestor_ids

    def _append_domain_item(
        self,
        parent_item: QTreeWidgetItem,
        domain_item: DomainItem,
    ) -> None:
        """Append one typed item and recursively project folder descendants."""
        tree_item = QTreeWidgetItem([domain_item.name])
        tree_item.setData(0, Qt.ItemDataRole.UserRole, domain_item)
        parent_item.addChild(tree_item)

        if isinstance(domain_item, ConnectionProfile):
            self._items_by_profile_id[domain_item.profile_id] = tree_item
            tree_item.setFlags(
                (tree_item.flags() | Qt.ItemFlag.ItemIsDragEnabled) & ~Qt.ItemFlag.ItemIsDropEnabled
            )
            self._apply_profile_presentation(tree_item, domain_item)
            return

        self._items_by_folder_id[domain_item.folder_id] = tree_item
        self._refresh_folder_icon(tree_item)
        tree_item.setFlags(
            tree_item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        )
        tree_item.setToolTip(
            0,
            tr("Connection folder | {p0} item(s)", p0=f"{len(domain_item.child_ids)}"),
        )
        tree_item.setData(
            0,
            Qt.ItemDataRole.AccessibleDescriptionRole,
            tr("Connection folder. Press Enter to expand or collapse it."),
        )
        for child in self._catalog.children_of(domain_item.folder_id):
            self._append_domain_item(tree_item, child)

    def _expand_ancestors(self, item_id: UUID) -> None:
        """Reveal a selected nested item without changing unrelated folders."""
        parent_id = self._catalog.parent_id_of(item_id)
        while parent_id is not None:
            parent_item = self._items_by_folder_id[parent_id]
            parent_item.setExpanded(True)
            parent_id = self._catalog.parent_id_of(parent_id)

    def _item_by_id(self, item_id: UUID) -> QTreeWidgetItem | None:
        """Resolve either typed item through stable identity maps."""
        return self._items_by_folder_id.get(item_id) or self._items_by_profile_id.get(item_id)

    def _current_domain_item(self) -> DomainItem | None:
        """Return a visible selected object and ignore hidden or unavailable rows."""
        current_item = self.sessions_tree.currentItem()
        if not self.sessions_tree.isEnabled() or current_item is None or current_item.isHidden():
            return None
        domain_item = (
            current_item.data(0, Qt.ItemDataRole.UserRole) if current_item is not None else None
        )
        return (
            domain_item if isinstance(domain_item, (ConnectionFolder, ConnectionProfile)) else None
        )

    def _request_new_connection(self, _checked: bool = False) -> None:
        """Request a profile beside the selection or inside the selected folder."""
        if self.sessions_tree.isEnabled():
            self.new_connection_requested.emit(self._selected_destination_folder_id())

    def _request_new_folder(self, _checked: bool = False) -> None:
        """Request a folder beside the selection or inside the selected folder."""
        if self.sessions_tree.isEnabled():
            self.new_folder_requested.emit(self._selected_destination_folder_id())

    def _selected_destination_folder_id(self) -> UUID | None:
        """Resolve the creation parent from the selected domain item."""
        domain_item = self._current_domain_item()
        if isinstance(domain_item, ConnectionFolder):
            return domain_item.folder_id
        if isinstance(domain_item, ConnectionProfile):
            return self._catalog.parent_id_of(domain_item.profile_id)
        return None

    def _activate_item(self, item: QTreeWidgetItem, _column: int) -> None:
        """Open profiles and leave Qt's normal mouse folder behavior intact."""
        if not self.sessions_tree.isEnabled() or item.isHidden():
            return
        domain_item = item.data(0, Qt.ItemDataRole.UserRole)
        if (
            isinstance(domain_item, ConnectionProfile)
            and self._items_by_profile_id.get(domain_item.profile_id) is item
        ):
            self.connection_activated.emit(domain_item)

    def _activate_current_item(self) -> None:
        """Open a profile or toggle one folder from the keyboard."""
        domain_item = self._current_domain_item()
        if isinstance(domain_item, ConnectionProfile):
            self.connection_activated.emit(domain_item)
        elif isinstance(domain_item, ConnectionFolder):
            folder_item = self._items_by_folder_id[domain_item.folder_id]
            folder_item.setExpanded(not folder_item.isExpanded())

    def _edit_current_item(self) -> None:
        """Route F2 to profile editing or folder renaming by type."""
        domain_item = self._current_domain_item()
        if isinstance(domain_item, ConnectionProfile):
            self.edit_connection_requested.emit(domain_item)
        elif isinstance(domain_item, ConnectionFolder):
            self.rename_folder_requested.emit(domain_item.folder_id)

    def _delete_current_item(self) -> None:
        """Route Delete to typed coordinator requests without local mutation."""
        domain_item = self._current_domain_item()
        if isinstance(domain_item, ConnectionProfile):
            self.delete_connection_requested.emit(domain_item)
        elif isinstance(domain_item, ConnectionFolder):
            self.delete_folder_requested.emit(domain_item.folder_id)

    def _move_current_item_up(self) -> None:
        """Request one keyboard-accessible step toward the start of the siblings."""
        self._emit_sibling_move(self._current_domain_item(), offset=-1)

    def _move_current_item_down(self) -> None:
        """Request one keyboard-accessible step toward the end of the siblings."""
        self._emit_sibling_move(self._current_domain_item(), offset=1)

    def _emit_sibling_move(
        self,
        domain_item: DomainItem | None,
        *,
        offset: int,
    ) -> None:
        """Emit one checked adjacent reorder request for a typed item."""
        request = self._sibling_move_request(domain_item, offset=offset)
        if request is not None:
            self.move_item_requested.emit(*request)

    def _sibling_move_request(
        self,
        domain_item: DomainItem | None,
        *,
        offset: int,
    ) -> MoveRequest | None:
        """Calculate an adjacent move using coordinates after source removal."""
        if self._filter_is_active() or domain_item is None or offset not in {-1, 1}:
            return None
        item_id = _domain_item_id(domain_item)
        parent_folder_id = self._catalog.parent_id_of(item_id)
        sibling_ids = tuple(
            _domain_item_id(sibling) for sibling in self._catalog.children_of(parent_folder_id)
        )
        current_index = sibling_ids.index(item_id)
        insertion_index = current_index + offset
        if not 0 <= insertion_index < len(sibling_ids):
            return None
        return item_id, parent_folder_id, insertion_index

    def _queue_dropped_move(
        self,
        item_id: object,
        target_item_id: object,
        position: object,
    ) -> None:
        """Defer a raw drop until Qt has completely unwound its drag stack."""
        if self._filter_is_active():
            return
        self._queued_drop_requested.emit(
            item_id,
            target_item_id,
            position,
            self._catalog_generation,
            self._catalog,
        )

    def _deliver_dropped_move(
        self,
        item_id: object,
        target_item_id: object,
        position: object,
        expected_generation: object,
        expected_catalog: object,
    ) -> None:
        """Validate one queued drop only if its originating snapshot is still current."""
        if (
            self._filter_is_active()
            or type(expected_generation) is not int
            or expected_generation != self._catalog_generation
            or expected_catalog is not self._catalog
        ):
            return
        if not isinstance(item_id, UUID):
            return
        if target_item_id is not None and not isinstance(target_item_id, UUID):
            return
        if not isinstance(position, DropPosition):
            return
        request = _calculate_drop_move_request(
            self._catalog,
            item_id,
            target_item_id,
            position,
        )
        if request is not None:
            self.move_item_requested.emit(*request)

    def _move_to_destinations(
        self,
        domain_item: DomainItem,
    ) -> tuple[tuple[UUID | None, str, int], ...]:
        """List valid alternate parents while excluding cycles and no-op parents."""
        if self._filter_is_active():
            return ()
        item_id = _domain_item_id(domain_item)
        current_parent_id = self._catalog.parent_id_of(item_id)
        choices: list[tuple[UUID | None, str]] = [(None, tr("User sessions"))]
        choices.extend(self._catalog.folder_choices())
        destinations: list[tuple[UUID | None, str, int]] = []
        for destination_parent_id, label in choices:
            if destination_parent_id == current_parent_id:
                continue
            insertion_index = _append_index_after_removal(
                self._catalog,
                item_id,
                destination_parent_id,
            )
            try:
                self._catalog.with_item_moved(
                    item_id,
                    parent_folder_id=destination_parent_id,
                    index=insertion_index,
                )
            except ValueError:
                continue
            destinations.append((destination_parent_id, label, insertion_index))
        return tuple(destinations)

    def _show_connection_context_menu(self, position: QPoint) -> None:
        """Build commands appropriate for blank, folder, or profile context."""
        tree = self.sessions_tree
        if not tree.isEnabled():
            return
        popup_position = position
        if tree.viewport().rect().contains(position):
            item = tree.itemAt(position)
        else:
            # Qt represents keyboard/assistive context-menu requests with an
            # invalid local point. Route those requests to the current row;
            # a real mouse click on valid blank space still gets the root menu.
            item = tree.currentItem()
            if item is not None:
                item_rect = tree.visualItemRect(item)
                popup_position = item_rect.bottomLeft() if item_rect.isValid() else QPoint()
            else:
                popup_position = QPoint()
        domain_item = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if item is not None and item.isHidden():
            return
        if item is not None and isinstance(
            domain_item,
            (ConnectionFolder, ConnectionProfile),
        ):
            if self._item_by_id(_domain_item_id(domain_item)) is not item:
                return
            self.sessions_tree.setCurrentItem(item)

        context_catalog = self._catalog
        context_generation = self._catalog_generation
        menu = QMenu(self)
        actions: dict[str, object] = {}
        move_destinations_by_action: list[tuple[object, UUID | None, int]] = []
        if isinstance(domain_item, ConnectionProfile):
            actions["open"] = menu.addAction(tr("Open"))
            actions["edit"] = menu.addAction(tr("Edit"))
            actions["duplicate"] = menu.addAction(tr("Duplicate"))
            favorite_target = not domain_item.is_favorite
            actions["favorite"] = menu.addAction(
                tr("Add to favorites") if favorite_target else tr("Remove from favorites")
            )
            menu.addSeparator()
            actions["export"] = menu.addAction(tr("Export connection…"))
            menu.addSeparator()
            actions["delete"] = menu.addAction(tr("Delete"))
        elif isinstance(domain_item, ConnectionFolder):
            actions["new_connection"] = menu.addAction(tr("New connection"))
            actions["new_folder"] = menu.addAction(tr("New subfolder"))
            menu.addSeparator()
            actions["rename"] = menu.addAction(tr("Rename folder"))
            menu.addSeparator()
            actions["export"] = menu.addAction(tr("Export folder…"))
            menu.addSeparator()
            actions["delete_folder"] = menu.addAction(tr("Delete folder"))
        else:
            actions["new_connection"] = menu.addAction(tr("New connection"))
            actions["new_folder"] = menu.addAction(tr("New folder"))

        if isinstance(domain_item, (ConnectionFolder, ConnectionProfile)):
            move_up_request = self._sibling_move_request(domain_item, offset=-1)
            move_down_request = self._sibling_move_request(domain_item, offset=1)
            destinations = self._move_to_destinations(domain_item)
            if move_up_request is not None or move_down_request is not None or destinations:
                menu.addSeparator()
            if move_up_request is not None:
                actions["move_up"] = menu.addAction(tr("Move up"))
            if move_down_request is not None:
                actions["move_down"] = menu.addAction(tr("Move down"))
            if destinations:
                move_to_menu = menu.addMenu(tr("Move to"))
                for destination_parent_id, label, insertion_index in destinations:
                    move_action = move_to_menu.addAction(escape_qt_mnemonic(label))
                    move_destinations_by_action.append(
                        (move_action, destination_parent_id, insertion_index)
                    )

        try:
            selected_action = menu.exec(tree.viewport().mapToGlobal(popup_position))
            if selected_action is None:
                return
            if selected_action in (actions.get("new_connection"), actions.get("new_folder")) and (
                not tree.isEnabled()
                or context_catalog is not self._catalog
                or context_generation != self._catalog_generation
            ):
                return
            if selected_action is actions.get("open"):
                current_item = self._items_by_profile_id.get(domain_item.profile_id)
                if current_item is not None:
                    self._activate_item(current_item, 0)
            elif selected_action is actions.get("edit"):
                self.edit_connection_requested.emit(domain_item)
            elif selected_action is actions.get("duplicate"):
                current_profile = (
                    self._catalog.profile_by_id(domain_item.profile_id)
                    if isinstance(domain_item, ConnectionProfile) and self.sessions_tree.isEnabled()
                    else None
                )
                if current_profile is not None:
                    self.duplicate_connection_requested.emit(current_profile)
            elif selected_action is actions.get("favorite"):
                current_profile = (
                    self._catalog.profile_by_id(domain_item.profile_id)
                    if isinstance(domain_item, ConnectionProfile) and tree.isEnabled()
                    else None
                )
                if (
                    current_profile is not None
                    and self.catalog_item_is_visible(current_profile.profile_id)
                    and current_profile.is_favorite != favorite_target
                ):
                    self.favorite_connection_requested.emit(
                        current_profile.profile_id, favorite_target
                    )
            elif selected_action is actions.get("export"):
                self._request_context_export(
                    item,
                    domain_item,
                    context_catalog,
                    context_generation,
                )
            elif selected_action is actions.get("delete"):
                self.delete_connection_requested.emit(domain_item)
            elif selected_action is actions.get("new_connection"):
                destination = (
                    domain_item.folder_id if isinstance(domain_item, ConnectionFolder) else None
                )
                self.new_connection_requested.emit(destination)
            elif selected_action is actions.get("new_folder"):
                parent_id = (
                    domain_item.folder_id if isinstance(domain_item, ConnectionFolder) else None
                )
                self.new_folder_requested.emit(parent_id)
            elif selected_action is actions.get("rename"):
                self.rename_folder_requested.emit(domain_item.folder_id)
            elif selected_action is actions.get("delete_folder"):
                self.delete_folder_requested.emit(domain_item.folder_id)
            elif selected_action is actions.get("move_up"):
                self._emit_sibling_move(domain_item, offset=-1)
            elif selected_action is actions.get("move_down"):
                self._emit_sibling_move(domain_item, offset=1)
            else:
                destination = next(
                    (
                        (parent_id, insertion_index)
                        for action, parent_id, insertion_index in move_destinations_by_action
                        if selected_action is action
                    ),
                    None,
                )
                if destination is not None:
                    self.move_item_requested.emit(
                        _domain_item_id(domain_item),
                        destination[0],
                        destination[1],
                    )
        finally:
            schedule_delete_later(menu)

    def _request_context_export(
        self,
        item: QTreeWidgetItem | None,
        domain_item: object,
        expected_catalog: ConnectionCatalog,
        expected_generation: int,
    ) -> None:
        """Export one unchanged visible context, never a fallback or filtered subset."""
        if (
            not isinstance(domain_item, (ConnectionFolder, ConnectionProfile))
            or expected_catalog is not self._catalog
            or expected_generation != self._catalog_generation
            or not self.sessions_tree.isEnabled()
        ):
            return
        item_id = _domain_item_id(domain_item)
        if (
            item is None
            or self._item_by_id(item_id) is not item
            or self.sessions_tree.currentItem() is not item
            or item.isDisabled()
            or item.data(0, Qt.ItemDataRole.UserRole) is not domain_item
        ):
            return
        visible_item = item
        while visible_item is not None:
            if visible_item.isHidden():
                return
            visible_item = visible_item.parent()
            if visible_item is not None and not visible_item.isExpanded():
                return
        self.export_item_requested.emit(item_id)

    def _show_empty_state(self) -> None:
        """Show one inert placeholder only for a completely empty catalog."""
        if self._empty_item is not None:
            return
        self._empty_item = self._inert_placeholder(
            tr("No saved sessions"),
            tr("No saved connection folders or profiles."),
        )
        self._sessions_root.addChild(self._empty_item)

    def _show_no_matches_state(self) -> None:
        """Append a distinct inert row without disturbing hidden domain items."""
        if self._favorites_only and not self._filter_terms:
            label = tr("No favorites yet")
            description = tr(
                "No connections are marked as favorites. Turn off Favorites, then use Add to favorites in a connection's context menu."
            )
        else:
            label = tr("Try a different search")
            description = (
                tr(
                    "No favorite connections match the search. Change the query or turn off Favorites."
                )
                if self._favorites_only
                else tr("No saved folders or connections match the filter. Try a different search.")
            )
        if self._no_matches_item is None:
            self._no_matches_item = self._inert_placeholder(label, description)
            self._sessions_root.addChild(self._no_matches_item)
        else:
            self._no_matches_item.setText(0, label)
            self._no_matches_item.setData(0, Qt.ItemDataRole.AccessibleDescriptionRole, description)

    def _remove_no_matches_state(self) -> None:
        """Remove only the transient no-result row from the persistent tree."""
        if self._no_matches_item is None:
            return
        item_index = self._sessions_root.indexOfChild(self._no_matches_item)
        if item_index >= 0:
            self._sessions_root.takeChild(item_index)
        self._no_matches_item = None

    @staticmethod
    def _inert_placeholder(label: str, accessible_description: str) -> QTreeWidgetItem:
        """Create a noninteractive synthetic row with accessible explanation."""
        placeholder = QTreeWidgetItem([label])
        placeholder.setFlags(
            placeholder.flags()
            & ~Qt.ItemFlag.ItemIsSelectable
            & ~Qt.ItemFlag.ItemIsEnabled
            & ~Qt.ItemFlag.ItemIsDragEnabled
            & ~Qt.ItemFlag.ItemIsDropEnabled
        )
        placeholder.setData(
            0,
            Qt.ItemDataRole.AccessibleDescriptionRole,
            accessible_description,
        )
        return placeholder

    def _apply_profile_presentation(
        self,
        item: QTreeWidgetItem,
        profile: ConnectionProfile,
    ) -> None:
        """Show only name/icon; retain endpoint details in safe tooltip/accessibility."""
        item.setIcon(0, self._profile_icon(profile))
        display_host = f"[{profile.host}]" if ":" in profile.host else profile.host
        endpoint = f"{display_host}:{profile.port}"
        tooltip_suffix = ""
        accessible_suffix = ""
        if profile.protocol is ConnectionProtocol.TELNET:
            encoding_label = telnet_encoding_label(profile.telnet_encoding)
            tooltip_suffix = f" | {encoding_label}"
            accessible_suffix = tr(" using {p0}", p0=f"{encoding_label}")
        elif profile.protocol is ConnectionProtocol.RDP:
            options = profile.rdp_options
            tooltip_suffix = f" | {options.width}x{options.height}"
            if options.view_only:
                tooltip_suffix += " | " + tr("View only")
        item.setToolTip(
            0,
            plain_text_tooltip(
                f"{'Favorite | ' if profile.is_favorite else ''}"
                f"{profile.protocol.upper()} | {endpoint}{tooltip_suffix}"
            ),
        )
        item.setData(
            0,
            Qt.ItemDataRole.AccessibleDescriptionRole,
            tr(
                "{p0}{p1} connection to {p2}{p3}.",
                p0=f"{('Favorite. ' if profile.is_favorite else '')}",
                p1=f"{profile.protocol.upper()}",
                p2=f"{endpoint}",
                p3=f"{accessible_suffix}",
            ),
        )
