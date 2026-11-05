"""FTP/explicit FTPS through Python's protocol engine, outside the GUI thread."""

import ftplib
import io
import os
import posixpath
import re
import select
import ssl
import time
from uuid import uuid4

import truststore

from termana.file_transfer.worker import FileWorker, JobCancelled, OverwriteRequired
from termana.models.connection import ConnectionProtocol
from termana.models.sftp import (
    MAX_SFTP_ENTRIES,
    MAX_SFTP_LISTING_NAME_BYTES,
    SFTP_CHUNK_BYTES,
    SftpEntry,
    SftpOperation,
    SftpProgress,
    SftpResult,
)
from termana.sftp.sftp_local_files import open_sftp_source
from termana.sftp.sftp_tree_local import local_directory


def create_tls_context():
    """Use system trust for FTPS without changing the application's HTTPS context."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class _OwnedControlReader(io.RawIOBase):
    """Poll owned control reads; leave lines/encoding/protocol parsing to stdlib.

    Windows may keep a socket.makefile read blocked after another thread closes
    its socket. Catch short recv timeouts below BufferedReader so its stream is
    not poisoned by a timeout, retaining the original per-read deadline.
    """

    def __init__(self, sock, owner):
        super().__init__()
        self._socket, self._owner = sock, owner

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("Read of closed FTP control stream")
        if not buffer:
            return 0
        sock, owner = self._socket, self._owner
        original_timeout = sock.gettimeout()
        deadline = time.monotonic() + original_timeout if original_timeout is not None else float("inf")
        try:
            while True:
                with owner._lock:
                    cancelled = owner._stop.is_set() or owner._active in owner._cancelled
                if cancelled:
                    raise JobCancelled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                sock.settimeout(min(0.1, remaining))
                try:
                    return sock.recv_into(buffer)
                except TimeoutError:
                    continue
        finally:
            try:
                sock.settimeout(original_timeout)
            except OSError:
                # Cancellation can close the exact owned socket concurrently.
                pass


class _OwnedTlsContext:
    """Track TLS sockets before the handshake so cancellation can release them."""
    def __init__(self, context, owner):
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("FTPS requires certificate and hostname verification")
        self._context, self._owner = context, owner

    def __getattr__(self, name):
        return getattr(self._context, name)

    def wrap_socket(self, sock, **options):
        wrapped = self._context.wrap_socket(sock, do_handshake_on_connect=False, **options)
        self._owner._track_socket(wrapped)
        timeout = wrapped.gettimeout()
        deadline = time.monotonic() + timeout if timeout is not None else float("inf")
        wrapped.setblocking(False)
        try:
            while True:
                if self._owner._stop.is_set():
                    raise JobCancelled
                if time.monotonic() >= deadline:
                    raise TimeoutError
                try:
                    wrapped.do_handshake()
                    break
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError) as error:
                    reading = isinstance(error, ssl.SSLWantReadError)
                    select.select([wrapped] if reading else [], [] if reading else [wrapped], [],
                                  min(0.1, max(0, deadline - time.monotonic())))
            wrapped.settimeout(timeout)
        except BaseException:
            wrapped.close()
            raise
        return wrapped


def _remote_path(path):
    if not path or any(c in path for c in "\r\n\0"):
        raise ValueError("Unsupported FTP path")
    return path


def _entry(name, facts):
    if not name or name in (".", "..") or any(c in name for c in "/\\\r\n\0"):
        return None
    kind = {"file": "file", "dir": "directory"}.get(facts.get("type"), "unknown")
    size = facts.get("size")
    size = int(size) if size is not None and size.isdecimal() else None
    return SftpEntry(name, kind, size)


def parse_ftp_list_line(line):
    """Recognize bounded Unix/DOS LIST rows; never turn an unknown row into absence."""
    if re.fullmatch(r"total\s+\d+", line):
        return None
    unix = re.fullmatch(
        r"([bcdlps-])[rwxstST-]{9}[+@.]?\s+\d+\s+\S+\s+\S+\s+(\d+)"
        r"\s+\S+\s+\d+\s+\S+\s+(.+)", line,
    )
    if unix:
        mode, size, name = unix.groups()
        return _entry(name, {"type": {"d": "dir", "-": "file"}.get(mode), "size": size})
    dos = re.fullmatch(r"\d{2}-\d{2}-\d{2,4}\s+\d{2}:\d{2}[AP]M\s+(<DIR>|\d+)\s+(.+)", line, re.I)
    if dos:
        size, name = dos.groups()
        return _entry(name, {"type": "dir" if size.upper() == "<DIR>" else "file", "size": size})
    raise ValueError("Unsupported FTP listing format; no file absence can be inferred")


class FtpBackend(FileWorker):
    protocol = ConnectionProtocol.FTP
    file_capabilities = frozenset({"browse", "files", "folders", "rename", "mkdir", "delete"})

    def __init__(self, profile, username, parent=None):
        super().__init__(profile, username, parent)
        if (type(username) is not str or not username or len(username) > 128
                or any(c in username for c in "\r\n\0")):
            raise ValueError("FTP requires an explicit username")
        self._ftp = None
        self._tree_progress = None
        self.connection_note = (
            "FTPS · TLS protects control and file data. Cancelling an active transfer disconnects this file session."
            if profile.file_options.ftp_security == "explicit-tls"
            else "FTP · Unencrypted. Cancelling an active transfer disconnects this file session."
        )

    def _connect(self):
        owner = self

        class TrackedConnection:
            def getresp(self):
                # ftplib assigns the control socket before reading its banner.
                # Register it here so closing a tab also interrupts that wait.
                owner._track_socket(self.sock)
                if self.sock is not getattr(self, "_control_reader_socket", None):
                    # ftplib has just created an unused file for this socket
                    # (initial connect or AUTH TLS). Do not replace a buffered
                    # reader on subsequent responses or lose read-ahead bytes.
                    self.file.close()
                    self.file = io.TextIOWrapper(
                        io.BufferedReader(_OwnedControlReader(self.sock, owner)),
                        encoding=self.encoding,
                    )
                    self._control_reader_socket = self.sock
                return super().getresp()

            def ntransfercmd(self, *args, **kwargs):
                connection, size = super().ntransfercmd(*args, **kwargs)
                owner._track_socket(connection)
                return connection, size

        class PlainFTP(TrackedConnection, ftplib.FTP):
            pass

        class SecureFTP(TrackedConnection, ftplib.FTP_TLS):
            pass

        options = self.profile.file_options
        if options.ftp_security == "explicit-tls":
            context = _OwnedTlsContext(create_tls_context(), self)
            self._ftp = SecureFTP(context=context, timeout=options.timeout, encoding=options.ftp_encoding)
        else:
            self._ftp = PlainFTP(timeout=options.timeout, encoding=options.ftp_encoding)
        self._ftp.trust_server_pasv_ipv4_address = False
        self._ftp.connect(self.profile.host, self.profile.port)
        self._track_socket(self._ftp.sock)
        if options.ftp_security == "explicit-tls":
            # Verify the server before collecting a password. Never downgrade.
            self._ftp.auth()
            self._track_socket(self._ftp.sock)
        password = self._password()
        try:
            self._ftp.login(self.username, password)
        finally:
            password = None
        if options.ftp_security == "explicit-tls":
            self._ftp.prot_p()
        self._ftp.set_pasv(True)

    def _listing(self, path, request):
        entries, name_bytes = [], 0
        path = _remote_path(path)
        try:
            def machine_row(line):
                nonlocal name_bytes
                self._check_cancel(request)
                facts_text, name = line.split(" ", 1)
                facts = dict(item.split("=", 1) for item in facts_text.rstrip(";").split(";"))
                facts = {key.lower(): value for key, value in facts.items()}
                if facts.get("type") in ("cdir", "pdir"):
                    return
                entry = _entry(name, facts)
                if entry is None:
                    raise ValueError("Unsupported FTP entry name")
                name_bytes += len(name.encode("utf-8"))
                if len(entries) >= MAX_SFTP_ENTRIES or name_bytes > MAX_SFTP_LISTING_NAME_BYTES:
                    raise ValueError("FTP directory exceeds the supported listing limit")
                entries.append(entry)
            self._ftp.retrlines("MLSD " + path, machine_row)
        except ftplib.error_perm as error:
            if str(error)[:3] not in ("500", "501", "502", "504"):
                raise
            def row(line):
                nonlocal name_bytes
                self._check_cancel(request)
                name_bytes += len(line.encode("utf-8"))
                if len(entries) >= MAX_SFTP_ENTRIES or name_bytes > MAX_SFTP_LISTING_NAME_BYTES:
                    raise ValueError("FTP directory exceeds the supported listing limit")
                entry = parse_ftp_list_line(line)
                if entry is not None:
                    entries.append(entry)
            self._ftp.retrlines("LIST " + path, row)
        return tuple(entries)

    def _stat(self, path, request):
        directory, name = posixpath.split(path)
        return next((e for e in self._listing(directory or "/", request) if e.name == name), None)

    def _download(self, request):
        local = request.local_path
        with local_directory(local.parent) as directory:
            expected = directory.lstat(local.name)
            if expected is not None and not request.overwrite:
                raise OverwriteRequired
            transferred = 0
            self._ftp.voidcmd("TYPE I")
            try:
                size = self._ftp.size(_remote_path(request.remote_path))
            except ftplib.error_perm as error:
                if str(error)[:3] not in ("500", "501", "502", "504"):
                    raise
                size = None
            with directory.writer(local.name, expected) as target:
                def chunk(data):
                    nonlocal transferred
                    self._check_cancel(request)
                    target.write(data)
                    transferred += len(data)
                    self._report_progress(SftpProgress(request.request_id, transferred, size or 0))
                self._ftp.retrbinary("RETR " + _remote_path(request.remote_path), chunk, SFTP_CHUNK_BYTES)
                self._check_cancel(request)
                if size is not None and transferred != size:
                    raise ValueError("Remote file size changed")

    def _upload(self, request, *, open_source=None):
        destination = _remote_path(request.remote_path)
        before = self._stat(destination, request)
        if before is not None and not request.overwrite:
            raise OverwriteRequired
        if before is not None and before.kind != "file":
            raise ValueError("FTP target is not a regular file")
        staged = posixpath.join(posixpath.dirname(destination), ".termana-" + uuid4().hex + ".part")
        created = False
        try:
            with (open_source() if open_source is not None else open_sftp_source(request.local_path)) as source:
                total, transferred = os.fstat(source.fileno()).st_size, 0
                created = True
                def progress(data):
                    nonlocal transferred
                    self._check_cancel(request)
                    transferred += len(data)
                    self._report_progress(SftpProgress(request.request_id, transferred, total))
                self._ftp.storbinary("STOR " + staged, source, SFTP_CHUNK_BYTES, progress)
            self._check_cancel(request)
            if self._stat(destination, request) != before:
                raise ValueError("FTP target changed before commit")
            # FTP has no universal exclusive rename/CAS. This is a reviewed
            # best-effort commit, never advertised as an atomic no-overwrite guarantee.
            self._ftp.rename(staged, destination)
            created = False
        finally:
            if created:
                self._ftp.delete(staged)

    def _perform(self, request):
        try:
            return self._execute(request)
        except OverwriteRequired:
            raise
        except Exception:
            # A failed/aborted data exchange may leave a final control reply.
            # Retire it rather than interpreting that reply as another command.
            self.close()
            raise

    def _report_progress(self, event):
        (self._tree_progress or self._publish)(event)

    def _execute(self, request):
        operation = request.operation
        path = _remote_path(request.remote_path)
        if operation.is_tree:
            from termana.file_transfer.ftp_tree import FtpTreeTransfer

            result = FtpTreeTransfer(self, request).run()
            if result.status in ("error", "cancelled"):
                self.close()
            return result
        if operation is SftpOperation.LIST:
            if path == ".":
                path = self._ftp.pwd()
            return SftpResult(request.request_id, "ok", directory=path, entries=self._listing(path, request))
        if operation is SftpOperation.MKDIR:
            if self._stat(path, request) is not None:
                raise OverwriteRequired
            self._ftp.mkd(path)
        elif operation is SftpOperation.RENAME:
            source = self._stat(path, request)
            if source is None or source.kind != "file":
                raise ValueError("Rename source changed or is not a regular file")
            if self._stat(request.target_path, request) is not None:
                raise OverwriteRequired
            self._ftp.rename(path, _remote_path(request.target_path))
        elif operation in (SftpOperation.DELETE_FILE, SftpOperation.REMOVE_DIRECTORY):
            entry = self._stat(path, request)
            directory = operation is SftpOperation.REMOVE_DIRECTORY
            if entry is None or entry.kind != ("directory" if directory else "file"):
                raise ValueError("The selected remote entry changed")
            (self._ftp.rmd if directory else self._ftp.delete)(path)
        elif operation is SftpOperation.DOWNLOAD:
            self._download(request)
        elif operation is SftpOperation.UPLOAD:
            self._upload(request)
        else:
            raise ValueError("This FTP operation is not available")
        with self._lock:
            self._sockets = {s for s in self._sockets if s.fileno() >= 0}
        return SftpResult(request.request_id, "ok", "Completed")

    def cancel_sftp(self, request_id):
        with self._lock:
            active = self._active == request_id
        accepted = super().cancel_sftp(request_id)
        if accepted and active:
            self.close()
        return accepted

    def _disconnect(self):
        try:
            if self._ftp is not None:
                self._ftp.close()
        finally:
            super()._disconnect()
