"""One independently owned file-client page in the existing pane workspace."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QDir, QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from termana.file_transfer.ftp import FtpBackend
from termana.file_transfer.jobs import SftpTransferQueue
from termana.file_transfer.local_files import LocalFilesModel
from termana.file_transfer.tftp import TftpBackend
from termana.models.connection import ConnectionProtocol
from termana.models.sftp import SftpOperation, SftpRequest, SftpTransferUpdate
from termana.sftp.widgets.sftp_browser import SftpBrowser
from termana.ssh.embedded_ssh import EmbeddedSshPlan
from termana.ssh.embedded_ssh_backend import EmbeddedSshBackend
from termana.ui.i18n import tr
from termana.ui.theme import DEFAULT_THEME
from termana.ui.ui_text import plain_text_tooltip
from termana.ui.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.ui.widgets.eliding_label import ElidingLabel
from termana.ui.widgets.wrapping_label import WrappingLabel


class FilePage(QWidget):
    state_changed = Signal()
    ready_to_delete = Signal()

    def __init__(self, profile, *, known_hosts, local_directory=None, backend_factory=None):
        super().__init__()
        self.profile = profile
        self.session_id = uuid4()
        self.terminal_name = self.display_title = profile.name
        self._known_hosts = known_hosts
        self._backend_factory = backend_factory
        self.backend = None
        self.browser = None
        self._closing = False
        self._delete_emitted = False
        self._focus_guard = lambda: True
        self._secret = None
        self._last_error = ""
        self._review_dialog = None
        self._tftp_jobs = {}
        self._colors = DEFAULT_THEME
        self._panel_fit_pending = False
        self._local_preference = None
        self._last_local_visible = None
        self.setProperty("sessionProtocol", profile.protocol.value)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        host = f"[{profile.host}]" if ":" in profile.host else profile.host
        caption = "FTPS" if profile.protocol is ConnectionProtocol.FTP and profile.file_options.ftp_security == "explicit-tls" else profile.protocol.upper()
        self._endpoint_caption = f"{caption} · {host}:{profile.port}"
        self.heading = ElidingLabel(self._endpoint_caption)
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setToolTip(plain_text_tooltip(self._endpoint_caption))
        layout.addWidget(self.heading)
        self.controls = QWidget()
        self.controls.setMinimumWidth(0)
        self.controls_layout = QVBoxLayout(self.controls)
        self.controls_layout.setContentsMargins(0, 0, 0, 0)
        self.controls_scroll = QScrollArea()
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.controls_scroll.setWidget(self.controls)
        layout.addWidget(self.controls_scroll)
        self.username = QLineEdit()
        self.username.setMaxLength(128)
        self.username.setPlaceholderText(tr("Username"))
        self.username.setAccessibleName(tr("File connection username"))
        self.username.setText(profile.username or "")
        self.username.setVisible(profile.protocol is not ConnectionProtocol.TFTP)
        self.connect_button = QPushButton(tr("Enable transfers") if profile.protocol is ConnectionProtocol.TFTP else tr("Connect"))
        self.connect_button.setProperty("actionRole", "primary")
        self.connect_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.disconnect_button = QPushButton(tr("Disconnect"))
        self.disconnect_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.disconnect_button.hide()
        self.disconnect_button.setEnabled(False)
        self.local_toggle = QPushButton(tr("Local files"))
        self.local_toggle.setCheckable(True)
        self.local_toggle.setVisible(profile.protocol is not ConnectionProtocol.TFTP)
        self.local_toggle.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.local_toggle.toggled.connect(self._toggle_local)
        self.connection_row = AdaptiveButtonRow((self.username, self.connect_button, self.disconnect_button, self.local_toggle))
        self.connection_row.row_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.controls_layout.addWidget(self.connection_row)
        self.status = WrappingLabel(tr("Enter a username and connect.") if profile.protocol is not ConnectionProtocol.TFTP else tr(
            "TFTP transfers a known filename. The server is contacted only when a transfer starts."
        ))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.controls_layout.addWidget(self.status)
        self.secret_row = QWidget()
        secret_layout = QVBoxLayout(self.secret_row)
        secret_layout.setContentsMargins(0, 0, 0, 0)
        self.secret_prompt = QPlainTextEdit()
        self.secret_prompt.setReadOnly(True)
        self.secret_prompt.setTabChangesFocus(True)
        self.secret_prompt.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.secret_prompt.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.secret_prompt.setAccessibleName(tr("Server identity or sign-in question"))
        self.secret_input = QLineEdit()
        self.secret_input.setMaxLength(1024)
        self.secret_submit = QPushButton(tr("Continue"))
        self.secret_cancel = QPushButton(tr("Cancel connection"))
        secret_layout.addWidget(self.secret_prompt)
        secret_layout.addWidget(AdaptiveButtonRow((self.secret_input, self.secret_submit, self.secret_cancel)))
        self.controls_layout.addWidget(self.secret_row)
        self.secret_row.hide()
        self.body = QVBoxLayout()
        layout.addLayout(self.body, 1)
        self.jobs = SftpTransferQueue(self)
        self.jobs.cancel_requested.connect(lambda backend, request_id: backend.cancel_sftp(request_id))
        self.jobs_scroll = QScrollArea()
        self.jobs_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.jobs_scroll.setWidgetResizable(True)
        self.jobs_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.jobs_scroll.setWidget(self.jobs)
        self.jobs_scroll.hide()
        layout.addWidget(self.jobs_scroll)
        if profile.protocol is ConnectionProtocol.TFTP:
            self._build_tftp()
        else:
            self._build_browser(local_directory or Path.home())
        self.connect_button.clicked.connect(self.connect_session)
        self.username.returnPressed.connect(self.connect_session)
        self.disconnect_button.clicked.connect(self.disconnect_session)
        self.secret_submit.clicked.connect(self._submit_secret)
        self.secret_input.returnPressed.connect(self._submit_secret)
        self.secret_cancel.clicked.connect(self.disconnect_session)
        self.controls.installEventFilter(self)
        for button in self.findChildren(QPushButton):
            if button.property("actionRole") is None:
                button.setProperty("actionRole", "secondary")
                button.style().unpolish(button)
                button.style().polish(button)
        self.jobs.installEventFilter(self)
        self._schedule_panel_fit()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.FontChange,
                            QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self._schedule_panel_fit()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "controls_scroll"):
            self._schedule_panel_fit()

    def _schedule_panel_fit(self):
        if not self._panel_fit_pending:
            self._panel_fit_pending = True
            QTimer.singleShot(0, self, self._fit_panels)

    def _toggle_local(self, checked):
        self._local_preference = checked
        self._schedule_panel_fit()

    def _fit_panels(self):
        self._panel_fit_pending = False
        if self._closing:
            return
        if hasattr(self, "local_panel"):
            visible = (self.width() >= self.fontMetrics().averageCharWidth() * 90
                       if self._local_preference is None else self._local_preference)
            self.local_panel.setVisible(visible)
            if visible and self._last_local_visible is not True:
                half = max(1, self.splitter.width() // 2)
                self.splitter.setSizes((half, half))
            self._last_local_visible = visible
            blocked = self.local_toggle.blockSignals(True)
            self.local_toggle.setChecked(visible)
            self.local_toggle.blockSignals(blocked)
        root = self.layout()
        lines = 4 if self._secret is not None and self._secret.is_host_confirmation else 2
        self.secret_prompt.setFixedHeight(self.secret_prompt.fontMetrics().lineSpacing() * lines + 12)
        margins = root.contentsMargins()
        body_minimum = max(self.body.minimumSize().height(), self.fontMetrics().lineSpacing() * 3)
        budget = max(self.fontMetrics().lineSpacing() * 2, self.height()
                     - margins.top() - margins.bottom() - root.spacing() * 3
                     - self.heading.sizeHint().height() - body_minimum)
        width = max(1, self.controls_scroll.viewport().width())
        desired = self.controls_layout.totalHeightForWidth(width)
        if desired < 0:
            desired = self.controls_layout.sizeHint().height()
        controls_height = min(max(1, desired), max(1, min(budget, int(self.height() * 0.45))))
        self.controls_scroll.setFixedHeight(controls_height)
        has_jobs = bool(self.jobs._entries)
        self.jobs_scroll.setVisible(has_jobs)
        if has_jobs:
            desired_jobs = self.jobs.layout().totalHeightForWidth(max(1, self.jobs_scroll.viewport().width()))
            if desired_jobs < 0:
                desired_jobs = self.jobs.sizeHint().height()
            # Expanded history must not consume the directory browser at large
            # fonts. The history itself remains vertically scrollable.
            self.jobs_scroll.setFixedHeight(max(1, min(
                desired_jobs, budget - controls_height, int(self.height() * 0.35))))

    @property
    def session_ended(self):
        return self.backend is not None and not self.backend.is_running

    @property
    def awaiting_login(self):
        return (self.backend is None or self._secret is not None
                or (self.backend.is_running and not self.backend.sftp_available))

    @property
    def needs_attention(self):
        return self._secret is not None

    @property
    def requires_close_confirmation(self):
        return (self.backend is not None and self.backend.is_running and (
            self.profile.protocol is not ConnectionProtocol.TFTP or self.backend.has_pending_jobs
        ))

    def set_focus_guard(self, guard):
        self._focus_guard = guard

    def focus_input(self):
        if self.secret_row.isVisible():
            target = self.secret_input
        elif self.backend is not None and self.backend.sftp_available:
            target = self.remote_filename if self.profile.protocol is ConnectionProtocol.TFTP else self.browser.files if self.browser is not None else self.disconnect_button
        elif self.backend is not None and self.backend.is_running:
            target = self.disconnect_button
        else:
            target = self.connect_button if self.profile.protocol is ConnectionProtocol.TFTP else self.username
        target.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def start(self):
        if self._focus_guard():
            self.focus_input()

    def _build_browser(self, directory):
        self.splitter = QSplitter()
        self.local_panel = QWidget()
        local_layout = QVBoxLayout(self.local_panel)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.addWidget(QLabel(tr("Local files")))
        self.local_path = QLineEdit(str(directory))
        self.local_path.setAccessibleName(tr("Local directory"))
        local_layout.addWidget(self.local_path)
        self.local_model = LocalFilesModel(self)
        self.local_model.setReadOnly(True)
        self.local_model.setResolveSymlinks(False)
        self.local_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.local_files = QTreeView()
        self.local_files.setModel(self.local_model)
        self.local_files.setMinimumWidth(0)
        self.local_files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_files.setDragEnabled(True)
        self.local_files.setRootIsDecorated(False)
        self.local_files.setRootIndex(self.local_model.setRootPath(str(directory)))
        self.local_files.hideColumn(2)
        self.local_files.hideColumn(3)
        self.local_files.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.local_files.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        local_layout.addWidget(self.local_files, 1)
        self.upload_selected = QPushButton(tr("Upload selected →"))
        self.upload_selected.setEnabled(False)
        local_layout.addWidget(self.upload_selected)
        self.local_files.doubleClicked.connect(self._local_open)
        self.local_path.returnPressed.connect(self._local_navigate)
        self.upload_selected.clicked.connect(self._upload_local_selection)
        self.remote_panel = QWidget()
        self.remote_layout = QVBoxLayout(self.remote_panel)
        self.remote_layout.setContentsMargins(0, 0, 0, 0)
        self.remote_layout.addWidget(QLabel(tr("Remote files")))
        self.splitter.addWidget(self.local_panel)
        self.splitter.addWidget(self.remote_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.body.addWidget(self.splitter)

    def _local_navigate(self):
        directory = Path(self.local_path.text()).expanduser()
        if directory.is_absolute() and directory.is_dir():
            self.local_files.setRootIndex(self.local_model.setRootPath(str(directory)))
        else:
            self.status.setText(tr("Choose an existing local directory."))

    def _local_open(self, index):
        if self.local_model.isDir(index):
            self.local_path.setText(self.local_model.filePath(index))
            self._local_navigate()

    def _upload_local_selection(self):
        if self.browser is not None and self.browser.available:
            paths = tuple(Path(self.local_model.filePath(i)) for i in self.local_files.selectionModel().selectedRows(0))
            self.browser.upload_paths(paths)

    def _build_tftp(self):
        fields = QWidget()
        form = QFormLayout(fields)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.remote_filename = QLineEdit()
        self.remote_filename.setMaxLength(1024)
        self.remote_filename.setPlaceholderText("configs/switch.cfg")
        self.remote_filename.setAccessibleName(tr("Remote TFTP filename"))
        form.addRow(tr("Remote filename"), self.remote_filename)
        self.tftp_upload = QPushButton(tr("Upload file…"))
        self.tftp_download = QPushButton(tr("Download file…"))
        self.tftp_upload.clicked.connect(lambda: self._tftp_transfer(SftpOperation.UPLOAD))
        self.tftp_download.clicked.connect(lambda: self._tftp_transfer(SftpOperation.DOWNLOAD))
        self.tftp_upload.setEnabled(False)
        self.tftp_download.setEnabled(False)
        form.addRow(AdaptiveButtonRow((self.tftp_upload, self.tftp_download)))
        self.body.addWidget(fields)
        self.body.addStretch()

    def _review(self, title, message):
        box = QMessageBox(self)
        self._review_dialog = box
        box.setWindowTitle(tr(title))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setProperty("actionRole", "primary")
        box.button(QMessageBox.StandardButton.Cancel).setProperty("actionRole", "secondary")
        for button in box.buttons():
            button.style().unpolish(button)
            button.style().polish(button)
        try:
            return box.exec() == QMessageBox.StandardButton.Yes and not self._closing
        finally:
            self._review_dialog = None
            box.deleteLater()

    def connect_session(self):
        if self._closing or (self.backend is not None and self.backend.is_running):
            return
        protocol = self.profile.protocol
        username = self.username.text() if protocol is ConnectionProtocol.FTP else self.username.text().strip()
        if protocol is not ConnectionProtocol.TFTP and not username.strip():
            self.status.setText(tr("Enter a username."))
            return
        if protocol is ConnectionProtocol.TFTP or (
            protocol is ConnectionProtocol.FTP and self.profile.file_options.ftp_security == "plain"
        ):
            warning = (
                tr("TFTP to {p0}:{p1} does not encrypt files or authenticate the server. Continue?",
                   p0=self.profile.host, p1=self.profile.port)
                if protocol is ConnectionProtocol.TFTP else
                tr("FTP to {p0}:{p1} sends the login and file data without encryption. Continue?",
                   p0=self.profile.host, p1=self.profile.port)
            )
            if not self._review("Unencrypted file connection", warning):
                return
        try:
            launch = replace(self.profile, username=username or None)
            if self._backend_factory is not None:
                backend = self._backend_factory(launch)
            elif protocol is ConnectionProtocol.SFTP:
                backend = EmbeddedSshBackend(
                    EmbeddedSshPlan(launch, self._known_hosts), uuid4(), self,
                    interactive_host_trust=True, file_only=True,
                )
                backend.connection_note = "SFTP · Independent verified SSH file connection"
            elif protocol is ConnectionProtocol.FTP:
                backend = FtpBackend(launch, username, self)
            else:
                backend = TftpBackend(launch, parent=self)
        except (OSError, RuntimeError, TypeError, ValueError):
            self.status.setText(tr("The file connection could not be prepared. Review the profile settings."))
            return
        if self.browser is not None:
            self.browser.dispose()
            self.browser.deleteLater()
            self.browser = None
        self.backend = backend
        self._last_error = ""
        backend.started.connect(lambda: self._connected(backend))
        backend.finished.connect(lambda code: self._finished(backend, code))
        backend.error_occurred.connect(lambda message: self._error(backend, message))
        backend.secret_requested.connect(lambda request: self._show_secret(backend, request))
        backend.secret_request_finished.connect(lambda request: self._hide_secret(backend, request))
        backend.sftp_progress.connect(lambda progress: self._tftp_progress(backend, progress))
        backend.sftp_result.connect(lambda result: self._tftp_result(backend, result))
        self.connect_button.setEnabled(False)
        self.connect_button.hide()
        self.username.hide()
        self.disconnect_button.setEnabled(True)
        self.disconnect_button.show()
        self.disconnect_button.setText(tr("Cancel connection"))
        self.username.setEnabled(False)
        self.status.setText(tr("Preparing file connection…"))
        backend.start()
        self._schedule_panel_fit()
        self.state_changed.emit()

    def _connected(self, backend):
        if backend is not self.backend or self._closing:
            return
        self.disconnect_button.setText(tr("Disable transfers") if self.profile.protocol is ConnectionProtocol.TFTP else tr("Disconnect"))
        if self.profile.protocol is ConnectionProtocol.TFTP:
            self.status.setText(tr("Ready for named-file transfers; server availability is checked per transfer."))
            self.tftp_upload.setEnabled(True)
            self.tftp_download.setEnabled(True)
        else:
            self.status.setText(backend.connection_note)
            self.browser = SftpBrowser(
                self.remote_panel, backend, self.display_title,
                local_directory=lambda: Path(self.local_path.text()).absolute(),
            )
            self.browser.set_interface_colors(self._colors)
            # The pinned page heading and status own the standalone identity.
            self.browser.owner_label.hide()
            self.browser.connection_badge.hide()
            self.browser.transfer_changed.connect(lambda job: self._record_transfer(backend, job))
            self.remote_layout.addWidget(self.browser)
            self.browser.navigate(".")
            self.upload_selected.setEnabled(True)
        self.state_changed.emit()
        self._schedule_panel_fit()

    def _show_secret(self, backend, request):
        if backend is not self.backend or self._closing:
            return
        self._secret = request
        self.secret_prompt.setPlainText(request.prompt)
        self.secret_input.clear()
        self.secret_input.setEchoMode(QLineEdit.EchoMode.Normal if request.is_host_confirmation else QLineEdit.EchoMode.Password)
        self.secret_input.setAccessibleName(tr("Server identity answer") if request.is_host_confirmation else tr("Connection secret"))
        self.secret_row.show()
        self.connection_row.hide()
        self.status.hide()
        self._schedule_panel_fit()
        self.state_changed.emit()
        if self.isVisible() and self._focus_guard():
            self.secret_input.setFocus()
            QTimer.singleShot(0, self, lambda: self.controls_scroll.ensureWidgetVisible(self.secret_input, 0, 8))

    def _hide_secret(self, backend, request):
        if backend is self.backend and self._secret is request:
            self._secret = None
            self.secret_input.clear()
            self.secret_row.hide()
            self.connection_row.show()
            self.status.show()
            self.state_changed.emit()

    def _submit_secret(self):
        if self._secret is not None and not self._closing:
            if self.backend.submit_secret(self._secret.request_id, self.secret_input.text()):
                self.secret_input.clear()

    def _error(self, backend, message):
        if backend is self.backend and not self._closing:
            self._last_error = message
            self.status.setText(message)

    def _finished(self, backend, _code):
        if backend is not self.backend:
            return
        self._secret = None
        self.secret_input.clear()
        self.secret_row.hide()
        self.connection_row.show()
        self.status.show()
        self.username.setEnabled(True)
        self.username.setVisible(self.profile.protocol is not ConnectionProtocol.TFTP)
        self.connect_button.setEnabled(not self._closing)
        self.connect_button.setVisible(not self._closing)
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.hide()
        if self.profile.protocol is ConnectionProtocol.TFTP:
            self.tftp_upload.setEnabled(False)
            self.tftp_download.setEnabled(False)
        else:
            self.upload_selected.setEnabled(False)
        if not self._closing:
            self.status.setText(self._last_error or tr("File connection closed. Connect again to start a new session."))
        self.state_changed.emit()
        self._schedule_panel_fit()
        if self._closing:
            self._ready_to_delete()

    def disconnect_session(self):
        self.secret_input.clear()
        if self.backend is not None:
            self.backend.close()

    def _tftp_transfer(self, operation):
        backend, filename = self.backend, self.remote_filename.text()
        if backend is None or not backend.sftp_available or not filename:
            self.status.setText(tr("Enable transfers and enter a remote filename."))
            return
        if operation is SftpOperation.UPLOAD:
            path, _ = QFileDialog.getOpenFileName(self, tr("Upload file"))
        else:
            path, _ = QFileDialog.getSaveFileName(self, tr("Download file"), "", options=QFileDialog.Option.DontConfirmOverwrite)
        if (not path or self._closing or backend is not self.backend or not backend.sftp_available
                or self.remote_filename.text() != filename):
            return
        overwrite = operation is SftpOperation.UPLOAD or Path(path).exists()
        if overwrite:
            message = (tr("TFTP cannot check whether {p0} already exists on the server. Upload may replace it. Continue?", p0=filename)
                       if operation is SftpOperation.UPLOAD else
                       tr("Replace the selected local file after the download completes?"))
            if not self._review("Confirm file replacement", message):
                return
        if (self._closing or backend is not self.backend or not backend.sftp_available
                or self.remote_filename.text() != filename):
            return
        try:
            request = SftpRequest(operation, filename, Path(path).absolute(), overwrite=overwrite)
        except ValueError:
            self.status.setText(tr("Choose a supported filename and local path."))
            return
        if backend.request_sftp(request):
            job = SftpTransferUpdate(request, tr("Queued"), True)
            self._tftp_jobs[(backend, request.request_id)] = job
            self._record_transfer(backend, job)
            self.state_changed.emit()

    def _tftp_progress(self, backend, progress):
        job = self._tftp_jobs.get((backend, progress.request_id))
        if job is not None and backend is self.backend:
            job = replace(job, transferred=progress.transferred, total=progress.total, status=tr("Transferring"))
            self._tftp_jobs[(backend, progress.request_id)] = job
            self._record_transfer(backend, job)

    def _tftp_result(self, backend, result):
        job = self._tftp_jobs.pop((backend, result.request_id), None)
        if job is not None:
            self._record_transfer(backend, replace(
                job, status=result.message or result.status, pending=False, result_status=result.status,
            ))
            self.state_changed.emit()

    def _record_transfer(self, backend, transfer):
        self.jobs.update_transfer(backend, self.display_title, transfer)
        # A scroll area's hidden child may already be logically shown. In that
        # case show() emits no ShowToParent event on Cocoa; refresh explicitly.
        self._schedule_panel_fit()

    def set_interface_colors(self, colors):
        self._colors = colors
        if self.browser is not None:
            self.browser.set_interface_colors(colors)

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        if self._review_dialog is not None:
            self._review_dialog.reject()
        self.secret_input.clear()
        if self.browser is not None:
            self.browser.dispose()
        if self.backend is not None and self.backend.is_running:
            self.backend.close()
        else:
            self._ready_to_delete()

    def _ready_to_delete(self):
        if not self._delete_emitted:
            self._delete_emitted = True
            self.ready_to_delete.emit()
