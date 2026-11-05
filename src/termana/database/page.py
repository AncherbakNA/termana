"""One SQL workspace: object navigator, query editor, results and transaction owner."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from termana.database.backend import DatabaseBackend
from termana.database.driver import table_query
from termana.database.results import QueryResult, validate_query
from termana.database.widgets import QueryTableModel, SqlHighlighter
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.ui.i18n import tr
from termana.ui.theme import DEFAULT_THEME
from termana.ui.ui_text import plain_text_tooltip
from termana.ui.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.ui.widgets.clipboard_text import copy_plain_text
from termana.ui.widgets.eliding_label import ElidingLabel
from termana.ui.widgets.wrapping_label import WrappingLabel


class DatabasePage(QWidget):
    state_changed = Signal()
    ready_to_delete = Signal()

    def __init__(self, profile, *, backend_factory=DatabaseBackend):
        super().__init__()
        if type(profile) is not ConnectionProfile or profile.protocol is not ConnectionProtocol.DATABASE:
            raise ValueError("Expected a database profile")
        self.profile = profile
        self.session_id = uuid4()
        self.terminal_name = self.display_title = profile.name
        self.backend = None
        self._backend_factory = backend_factory
        self._closing = self._delete_emitted = False
        self._focus_guard = lambda: True
        self._pending = None
        self._review_dialog = None
        self._last_error = ""
        self._busy = False
        self._transaction_open = False
        self._controls_fit_pending = False
        self.setProperty("sessionProtocol", "database")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        options = profile.database_options
        engine = {"postgresql": "PostgreSQL", "mysql": "MySQL / MariaDB", "sqlite": "SQLite"}[options.engine]
        host = f"[{profile.host}]" if ":" in profile.host else profile.host
        endpoint = (options.sqlite_path or tr("Choose a local file")) if options.engine == "sqlite" else f"{host}:{profile.port} / {options.database or tr('Server default')}"
        self.heading = ElidingLabel(engine + " · " + profile.name)
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setToolTip(plain_text_tooltip(engine + " · " + endpoint))
        heading_row = QHBoxLayout()
        heading_row.addWidget(self.heading, 1)
        self.mode_label = QLabel(tr("Read-only default") if options.read_only else tr("Writable"))
        self.mode_label.setToolTip(tr("The read-only transaction default is requested from the database. Account permissions remain authoritative.")
                                   if options.read_only else tr("Manual transactions; some DDL statements commit immediately."))
        heading_row.addWidget(self.mode_label)
        layout.addLayout(heading_row)
        controls = QWidget()
        controls.installEventFilter(self)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        self.username = QLineEdit()
        self.username.setMaxLength(128)
        self.username.setPlaceholderText(tr("Username"))
        self.username.setAccessibleName(tr("Database username"))
        self.password = QLineEdit()
        self.password.setMaxLength(1024)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(tr("Password"))
        self.password.setAccessibleName(tr("Database password"))
        self.username.setVisible(options.engine != "sqlite")
        self.password.setVisible(options.engine != "sqlite")
        self.connect_button = QPushButton(tr("Open database") if options.engine == "sqlite" else tr("Connect"))
        self.connect_button.setProperty("actionRole", "primary")
        self.connect_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.disconnect_button = QPushButton(tr("Disconnect"))
        self.disconnect_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.disconnect_button.hide()
        heading_row.addWidget(self.disconnect_button)
        self.login_row = AdaptiveButtonRow((self.username, self.password, self.connect_button))
        self.login_row.row_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        controls_layout.addWidget(self.login_row)
        self.status = WrappingLabel(tr("Connect to browse tables and run SQL. Passwords, SQL and results stay in this session."))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        controls_layout.addWidget(self.status)
        self.controls_scroll = QScrollArea()
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.controls_scroll.setWidget(controls)
        layout.addWidget(self.controls_scroll)
        self.splitter = QSplitter()
        navigator = QWidget()
        navigator_layout = QVBoxLayout(navigator)
        navigator_layout.setContentsMargins(0, 0, 0, 0)
        navigator_layout.addWidget(QLabel(tr("Navigator")))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filter loaded tables"))
        self.filter.setMaxLength(256)
        self.filter.setClearButtonEnabled(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(0)
        self.tree.setAccessibleName(tr("Schemas, tables and columns"))
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setToolTip(tr("Refresh schemas and tables"))
        navigator_layout.addWidget(self.filter)
        navigator_layout.addWidget(self.tree, 1)
        navigator_layout.addWidget(self.refresh_button)
        self.splitter.addWidget(navigator)
        sql_workspace = QWidget()
        query_layout = QVBoxLayout(sql_workspace)
        query_layout.setContentsMargins(0, 0, 0, 0)
        self.run_button = QPushButton(tr("Run SQL"))
        self.run_button.setProperty("actionRole", "primary")
        self.run_button.setToolTip(tr("Execute the selection, or the whole editor as one statement (Ctrl/Cmd+Enter)."))
        self.stop_button = QPushButton(tr("Stop"))
        self.stop_button.setToolTip(tr("Stop waiting and disconnect this session. The server transaction state must be checked before retrying."))
        self.commit_button = QPushButton(tr("Commit"))
        self.commit_button.setToolTip(tr("Save the current transaction. Some database commands commit immediately."))
        self.rollback_button = QPushButton(tr("Rollback"))
        self.rollback_button.setToolTip(tr("Undo the current uncommitted transaction; already committed changes are not undone."))
        query_layout.addWidget(AdaptiveButtonRow((self.run_button, self.stop_button, self.commit_button, self.rollback_button)))
        self.query_splitter = QSplitter(Qt.Orientation.Vertical)
        self.editor = QPlainTextEdit()
        self.editor.setAccessibleName(tr("SQL query editor"))
        self.editor.setPlaceholderText("SELECT 1;")
        fixed_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed_font.setPointSizeF(max(1, self.font().pointSizeF()))
        self.editor.setFont(fixed_font)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.highlighter = SqlHighlighter(self.editor.document())
        self.query_splitter.addWidget(self.editor)
        results = QWidget()
        result_layout = QVBoxLayout(results)
        result_layout.setContentsMargins(0, 0, 0, 0)
        self.result_note = WrappingLabel(tr("Results appear here after execution. Up to 1000 rows are displayed."))
        self.result_note.setTextFormat(Qt.TextFormat.PlainText)
        self.result_model = QueryTableModel(self)
        self.result_grid = QTableView()
        self.result_grid.setModel(self.result_model)
        self.result_grid.setMinimumWidth(0)
        self.result_grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.result_grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.result_grid.horizontalHeader().setDefaultSectionSize(140)
        self.result_grid.setAccessibleName(tr("SQL result grid"))
        self.copy_button = QPushButton(tr("Copy selected cells"))
        for button in (self.disconnect_button, self.stop_button, self.commit_button,
                       self.rollback_button, self.refresh_button, self.copy_button):
            button.setProperty("actionRole", "secondary")
        self.copy_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        result_layout.addWidget(self.result_note)
        result_layout.addWidget(self.result_grid, 1)
        result_layout.addWidget(self.copy_button)
        self.query_splitter.addWidget(results)
        self.query_splitter.setStretchFactor(0, 1)
        self.query_splitter.setStretchFactor(1, 1)
        self.query_splitter.setSizes((240, 400))
        query_layout.addWidget(self.query_splitter, 1)
        self.splitter.addWidget(sql_workspace)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes((220, 800))
        layout.addWidget(self.splitter, 1)
        self.connect_button.clicked.connect(self.connect_session)
        self.username.returnPressed.connect(self.connect_session)
        self.password.returnPressed.connect(self.connect_session)
        self.disconnect_button.clicked.connect(self.disconnect_session)
        self.stop_button.clicked.connect(self.disconnect_session)
        self.run_button.clicked.connect(self.run_query)
        self.refresh_button.clicked.connect(lambda: self._submit("tables"))
        self.commit_button.clicked.connect(lambda: self._submit("commit"))
        self.rollback_button.clicked.connect(lambda: self._submit("rollback"))
        self.tree.itemDoubleClicked.connect(self._preview_table)
        self.tree.itemExpanded.connect(self._load_columns)
        self.filter.textChanged.connect(self._filter_tables)
        self.copy_button.clicked.connect(self._copy_cells)
        self.run_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.run_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.run_shortcut.activated.connect(self.run_query)
        self._update_actions()
        self.set_interface_colors(DEFAULT_THEME)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "controls_scroll"):
            self._schedule_controls_fit()

    def eventFilter(self, watched, event):
        if (hasattr(self, "controls_scroll") and watched is self.controls_scroll.widget()
                and event.type() == QEvent.Type.LayoutRequest):
            self._schedule_controls_fit()
        return super().eventFilter(watched, event)

    def _schedule_controls_fit(self):
        if not self._controls_fit_pending:
            self._controls_fit_pending = True
            QTimer.singleShot(0, self, self._fit_controls)

    def _fit_controls(self):
        self._controls_fit_pending = False
        controls = self.controls_scroll.widget()
        height = controls.heightForWidth(self.controls_scroll.viewport().width())
        if height < 0:
            height = controls.sizeHint().height()
        self.controls_scroll.setFixedHeight(min(height + 4, max(80, self.height() // 3)))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "editor"):
            font = self.editor.font()
            font.setPointSizeF(max(1, self.font().pointSizeF()))
            self.editor.setFont(font)
            self._schedule_controls_fit()

    @property
    def session_ended(self):
        return self.backend is not None and not self.backend.is_running

    @property
    def awaiting_login(self):
        return self.backend is None or (self.backend.is_running and not self.backend._ready)

    @property
    def needs_attention(self):
        return bool(self._last_error)

    @property
    def requires_close_confirmation(self):
        return (self.backend is not None and self.backend.is_running) or self.editor.document().isModified()

    def set_focus_guard(self, guard):
        self._focus_guard = guard

    def focus_input(self):
        target = self.editor if self.backend is not None and self.backend._ready else self.connect_button if self.profile.database_options.engine == "sqlite" else self.username
        target.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def start(self):
        if self._focus_guard():
            self.focus_input()

    def _review(self, message):
        box = QMessageBox(self)
        self._review_dialog = box
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle(tr("Database connection"))
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        try:
            return box.exec() == QMessageBox.StandardButton.Yes and not self._closing
        finally:
            self._review_dialog = None
            box.deleteLater()

    def connect_session(self):
        if self._closing or (self.backend is not None and self.backend.is_running):
            return
        options = self.profile.database_options
        profile = self.profile
        if options.engine == "sqlite" and options.sqlite_path is None:
            path, _ = QFileDialog.getOpenFileName(self, tr("Choose an existing SQLite database"))
            if not path or self._closing:
                return
            profile = replace(profile, database_options=replace(options, sqlite_path=str(Path(path).absolute())))
        if options.engine != "sqlite" and not self.username.text().strip():
            self.status.setText(tr("Enter a database username."))
            return
        if options.engine != "sqlite" and options.tls_mode == "disabled":
            if not self._review(tr("This database connection will send authentication and data without TLS. Continue?")):
                self.password.clear()
                return
        if not options.read_only and not self._review(tr("Open a writable database session? SQL can change data. Use Commit/Rollback for transactions; some DDL statements commit immediately.")):
            self.password.clear()
            return
        if self._closing:
            return
        try:
            backend = self._backend_factory(profile, self.username.text(), self.password.text())
        except (ValueError, OSError):
            self.password.clear()
            self.status.setText(tr("The database session could not be prepared. Review the profile."))
            return
        self.password.clear()
        backend.setParent(self)
        self.backend = backend
        self.tree.clear()
        self.result_model.replace_result(QueryResult())
        self.result_note.setText(tr("No results for this connection yet."))
        self._transaction_open = False
        if options.engine == "sqlite":
            self.heading.setToolTip(plain_text_tooltip("SQLite · " + (profile.database_options.sqlite_path or "")))
        self._last_error = ""
        backend.connected.connect(lambda: self._connected(backend))
        backend.result.connect(lambda request_id, kind, value: self._result(backend, request_id, kind, value))
        backend.failed.connect(lambda request_id, message: self._failed(backend, request_id, message))
        backend.finished.connect(lambda: self._finished(backend))
        for widget in (self.username, self.password, self.connect_button):
            widget.hide()
        self.disconnect_button.show()
        self.status.setText(tr("Connecting to database…"))
        backend.start()
        self._update_actions()
        self.state_changed.emit()

    def _connected(self, backend):
        if backend is not self.backend or self._closing:
            return
        readonly = self.profile.database_options.read_only
        self.status.setText(tr("Connected · read-only by default") if readonly else tr("Connected · manual transactions. Some DDL statements commit immediately."))
        self._update_actions()
        self._submit("tables")
        self.state_changed.emit()

    def _submit(self, kind, query="", context=None):
        backend = self.backend
        if self._closing or backend is None or self._busy or not backend.ready:
            return
        try:
            request_id = backend.submit(kind, query)
        except ValueError:
            self.status.setText(tr("Enter a valid SQL statement up to 128 KiB."))
            return
        if request_id is not None:
            self._pending = (request_id, kind, context)
            self._busy = True
            self._last_error = ""
            self.status.setText(tr("Running database operation…"))
            self._update_actions()
            self.state_changed.emit()

    def run_query(self):
        selected = self.editor.textCursor().selectedText().replace("\u2029", "\n")
        query = selected or self.editor.toPlainText()
        try:
            validate_query(query)
        except ValueError:
            self.status.setText(tr("Enter a valid SQL statement up to 128 KiB."))
            return
        self._submit("query", query)

    def _result(self, backend, request_id, kind, value):
        if backend is not self.backend or self._closing or self._pending is None or request_id != self._pending[0]:
            return
        context = self._pending[2]
        self._pending = None
        self._busy = False
        if kind == "tables":
            self.tree.clear()
            schemas = {}
            for schema, name, entry_type in value.rows:
                parent = schemas.get(schema)
                if parent is None:
                    parent = schemas[schema] = QTreeWidgetItem(self.tree, (schema,))
                    parent.setToolTip(0, plain_text_tooltip(schema))
                item = QTreeWidgetItem(parent, (name,))
                item.setToolTip(0, plain_text_tooltip(schema + "." + name))
                item.setData(0, Qt.ItemDataRole.UserRole, (schema, name, entry_type))
                QTreeWidgetItem(item, (tr("Expand to load columns"),))
            for item in schemas.values():
                item.setExpanded(True)
            self._transaction_open = value.transaction_open
            self.status.setText(tr("Loaded tables: {p0}", p0=len(value.rows)) + (tr(" · list truncated") if value.truncated else ""))
            self._filter_tables()
        elif kind == "columns":
            if context is not None and context.treeWidget() is self.tree:
                context.takeChildren()
                for row in value.rows:
                    caption = str(row[0]) + " · " + str(row[1])
                    column = QTreeWidgetItem(context, (caption,))
                    column.setToolTip(0, plain_text_tooltip(caption))
            self._transaction_open = value.transaction_open
            self.status.setText(tr("Columns loaded."))
        elif kind == "query":
            self.result_model.replace_result(value)
            self._transaction_open = value.transaction_open
            self.result_note.setText(tr("Last execution · {p0} displayed rows", p0=len(value.rows))
                                     if value.columns else tr("Last execution · affected rows: {p0}", p0=value.affected if value.affected is not None else "?"))
            if value.truncated:
                self.result_note.setText(self.result_note.text() + tr(" · preview truncated; use LIMIT or smaller columns"))
            self.status.setText(tr("Execution completed. Transaction open; choose Commit or Rollback.") if self._transaction_open else tr("Execution completed."))
        else:
            self._transaction_open = bool(value)
            self.status.setText(tr("Transaction committed.") if kind == "commit" else tr("Transaction rolled back."))
        self._update_actions()
        self.state_changed.emit()

    def _failed(self, backend, request_id, message):
        if backend is not self.backend or self._closing:
            return
        if request_id is not None and (self._pending is None or request_id != self._pending[0]):
            return
        self._pending = None
        self._busy = False
        self._last_error = message
        self.status.setText(message)
        self._update_actions()
        self.state_changed.emit()

    def _finished(self, backend):
        if backend is not self.backend:
            return
        self._busy = False
        self._pending = None
        self.password.clear()
        self.disconnect_button.hide()
        if not self._closing:
            local = self.profile.database_options.engine == "sqlite"
            self.username.setVisible(not local)
            self.password.setVisible(not local)
            self.connect_button.show()
            self.status.setText(self._last_error or tr("Database disconnected. Uncommitted changes are rolled back where supported; check the server state before retrying."))
        self._update_actions()
        self.state_changed.emit()
        if self._closing:
            self._ready_to_delete()

    def _update_actions(self):
        ready = self.backend is not None and self.backend.ready and not self._busy and not self._closing
        for widget in (self.run_button, self.refresh_button, self.tree):
            widget.setEnabled(ready)
        self.commit_button.setEnabled(ready)
        self.rollback_button.setEnabled(ready)
        self.stop_button.setEnabled(self.backend is not None and self.backend.is_running and not self._closing)
        self.connect_button.setEnabled(not self._closing and (self.backend is None or not self.backend.is_running))
        self.login_row.setVisible(not self.connect_button.isHidden())
        self._schedule_controls_fit()
        self.copy_button.setEnabled(bool(self.result_model.result.rows))

    def _filter_tables(self):
        needle = self.filter.text().casefold()
        for index in range(self.tree.topLevelItemCount()):
            schema = self.tree.topLevelItem(index)
            visible = False
            for number in range(schema.childCount()):
                item = schema.child(number)
                show = not needle or needle in item.text(0).casefold() or needle in schema.text(0).casefold()
                item.setHidden(not show)
                visible |= show
            schema.setHidden(not visible)

    def _preview_table(self, item, _column):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is not None:
            self.editor.setPlainText(table_query(self.profile.database_options.engine, data[0], data[1]))
            self.run_query()

    def _load_columns(self, item):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is not None and item.childCount() == 1 and item.child(0).text(0) == tr("Expand to load columns"):
            self._submit("columns", data, item)

    def _copy_cells(self):
        if self._closing:
            return
        indexes = self.result_grid.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows = {}
        for index in sorted(indexes, key=lambda i: (i.row(), i.column())):
            rows.setdefault(index.row(), []).append(self.result_model.data(index))
        if not copy_plain_text("\n".join("\t".join(row) for row in rows.values())):
            self.status.setText(tr("Clipboard unavailable."))

    def disconnect_session(self):
        self.password.clear()
        if self.backend is not None:
            self.backend.close()

    def set_interface_colors(self, colors):
        self.highlighter.set_colors(colors)

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self.password.clear()
        if self._review_dialog is not None:
            self._review_dialog.reject()
        if self.backend is not None and self.backend.is_running:
            self.backend.close()
        else:
            self._ready_to_delete()

    def _ready_to_delete(self):
        if not self._delete_emitted:
            self._delete_emitted = True
            self.ready_to_delete.emit()
