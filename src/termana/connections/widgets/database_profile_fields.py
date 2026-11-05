"""Database settings in the shared profile editor; passwords are runtime only."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from termana.models.database_options import DEFAULT_DATABASE_OPTIONS, DatabaseOptions
from termana.ui.i18n import tr
from termana.ui.widgets.form_controls import FormComboBox, FormSpinBox
from termana.ui.widgets.themed_checkbox import ThemedCheckBox


class DatabaseProfileFields(QWidget):
    changed = Signal()

    def __init__(self, parent=None, *, options=DEFAULT_DATABASE_OPTIONS):
        super().__init__(parent)
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.engine = FormComboBox()
        for label, value in (("PostgreSQL", "postgresql"), ("MySQL / MariaDB", "mysql"), ("SQLite", "sqlite")):
            self.engine.addItem(label, value)
        self.engine.setCurrentIndex(self.engine.findData(options.engine))
        self.database = QLineEdit(options.database)
        self.database.setMaxLength(128)
        self.database.setPlaceholderText(tr("Server default (optional)"))
        self.security = FormComboBox()
        self.security.addItem(tr("TLS — verify server certificate and name"), "verify-full")
        self.security.addItem(tr("Unencrypted — explicit consent required"), "disabled")
        self.security.setCurrentIndex(self.security.findData(options.tls_mode))
        self.sqlite_path = QLineEdit(options.sqlite_path or "")
        self.ca_file = QLineEdit(options.ca_file or "")
        self.timeout = FormSpinBox()
        self.timeout.setRange(1, 600)
        self.timeout.setSuffix(tr(" s"))
        self.timeout.setValue(options.timeout)
        self.query_timeout = FormSpinBox()
        self.query_timeout.setRange(1, 600)
        self.query_timeout.setSuffix(tr(" s"))
        self.query_timeout.setValue(options.query_timeout)
        self.read_only = ThemedCheckBox(tr("Start read-only"))
        self.read_only.setChecked(options.read_only)
        self.form.addRow(tr("Database engine"), self.engine)
        self._network_rows = []
        for text, control in (("Database", self.database), ("Security", self.security),
                              ("Trusted CA file (local)", self._path_row(self.ca_file, "Choose a trusted CA certificate"))):
            label = QLabel(tr(text))
            self.form.addRow(label, control)
            self._network_rows.extend((label, control))
        label = QLabel(tr("SQLite file (local)"))
        control = self._path_row(self.sqlite_path, "Choose an existing SQLite database")
        self.form.addRow(label, control)
        self._sqlite_rows = (label, control)
        self.form.addRow(tr("Connect timeout"), self.timeout)
        self.form.addRow(tr("Query timeout"), self.query_timeout)
        self.form.addRow(self.read_only)
        self.note = QLabel(tr("Sign in when the session opens. Local file paths stay on this device. Query results are not synced."))
        self.note.setWordWrap(True)
        self.form.addRow(self.note)
        self.engine.currentIndexChanged.connect(self._engine_changed)
        for field in (self.database, self.sqlite_path, self.ca_file):
            field.textChanged.connect(self.changed)
        for field in (self.timeout, self.query_timeout):
            field.valueChanged.connect(self.changed)
        self.security.currentIndexChanged.connect(self.changed)
        self.read_only.toggled.connect(self.changed)
        self._engine_changed()

    def _path_row(self, field, title):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        field.setMaxLength(4096)
        field.setMinimumWidth(0)
        field.setAccessibleName(tr(title))
        button = QPushButton(tr("Browse…"))
        def choose():
            path, _ = QFileDialog.getOpenFileName(self, tr(title))
            if path:
                field.setText(path)
        button.clicked.connect(choose)
        layout.addWidget(field, 1)
        layout.addWidget(button)
        return row

    def _engine_changed(self):
        local = self.engine.currentData() == "sqlite"
        for widget in self._network_rows:
            widget.setVisible(not local)
        for widget in self._sqlite_rows:
            widget.setVisible(local)
        self.changed.emit()

    @property
    def selected(self):
        local = self.engine.currentData() == "sqlite"
        return DatabaseOptions(
            engine=self.engine.currentData(), database="" if local else self.database.text(),
            tls_mode="verify-full" if local else self.security.currentData(),
            sqlite_path=(self.sqlite_path.text() or None) if local else None,
            ca_file=None if local else (self.ca_file.text() or None),
            timeout=self.timeout.value(), query_timeout=self.query_timeout.value(),
            read_only=self.read_only.isChecked(),
        )

    @property
    def selection_error(self):
        try:
            _ = self.selected
            return None
        except (ValueError, UnicodeError) as error:
            return str(error)
