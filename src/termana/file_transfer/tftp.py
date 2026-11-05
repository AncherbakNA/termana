"""Known-name TFTP transfers via TFTPy, never a simulated directory browser."""

import logging
import os
import time

from termana.file_transfer.worker import FileWorker, OverwriteRequired
from termana.models.connection import ConnectionProtocol
from termana.models.sftp import SftpOperation, SftpProgress, SftpResult
from termana.sftp.sftp_local_files import open_sftp_source
from termana.sftp.sftp_tree_local import local_directory


class _BorrowedFile:
    """The library may close its stream; the guarded local transaction owns it."""
    def __init__(self, stream):
        self._stream = stream

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def close(self):
        pass


class _ScopedDatagram:
    """Cancellation and endpoint ownership around the engine's UDP socket.

    TFTPy 0.8.7 logs unexpected peers but can still dispatch their packets.
    Filter them before the library state machine; packet decoding, TFTP state,
    acknowledgements and retransmission remain entirely library-owned.
    """
    def __init__(self, context, check_cancel, timeout):
        self._socket = context.sock
        self._context = context
        self._check_cancel = check_cancel
        self._timeout = timeout

    def __getattr__(self, name):
        return getattr(self._socket, name)

    def recvfrom(self, size):
        deadline = time.monotonic() + self._timeout
        while True:
            self._check_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self._socket.settimeout(min(0.1, remaining))
            try:
                data, peer = self._socket.recvfrom(size)
            except TimeoutError:
                continue
            if peer[0] != self._context.address:
                continue
            if self._context.tidport is not None and peer[1] != self._context.tidport:
                continue
            return data, peer


class TftpBackend(FileWorker):
    protocol = ConnectionProtocol.TFTP
    file_capabilities = frozenset({"files"})
    connection_note = "TFTP · Ready for named-file transfers; server availability is checked per transfer"

    def _connect(self):
        import tftpy

        if tftpy.__version__ != "0.8.7":
            raise RuntimeError("The TFTP engine requires a compatibility review")
        # Engine diagnostics can include remote paths and peer-provided text.
        # Only our fixed outcome categories enter the application UI/logs.
        logger = logging.getLogger("tftpy")
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())
        logger.propagate = False

    def _perform(self, request):
        from tftpy.TftpContexts import TftpContextClientDownload, TftpContextClientUpload
        from tftpy.TftpShared import TftpFileNotFoundError, TftpTimeout

        if request.operation not in (SftpOperation.DOWNLOAD, SftpOperation.UPLOAD):
            raise ValueError("TFTP supports only named-file upload and download")
        filename = request.remote_path
        if any(c in filename for c in "\r\n\0") or len(filename.encode("utf-8")) > 1024:
            raise ValueError("Unsupported TFTP filename")
        options = self.profile.file_options
        def progress(_packet):
            self._check_cancel(request)
            self._publish(SftpProgress(request.request_id, stream.tell(), total))
        def transfer(context_class, stream):
            context = context_class(
                self.profile.host, self.profile.port, filename, _BorrowedFile(stream),
                {"blksize": options.tftp_block_size}, progress, options.timeout,
                retries=options.tftp_retries, flock=False,
            )
            context.sock = _ScopedDatagram(context, lambda: self._check_cancel(request), options.timeout)
            self._context = context
            self._track_socket(context.sock)
            try:
                with context:
                    self._check_cancel(request)
                    context.start()
            except TftpTimeout:
                raise TimeoutError from None
            except TftpFileNotFoundError:
                raise FileNotFoundError from None
        if request.operation is SftpOperation.UPLOAD:
            # TFTP has no existence query: every PUT needs explicit replacement consent.
            if not request.overwrite:
                raise OverwriteRequired
            with open_sftp_source(request.local_path) as stream:
                total = os.fstat(stream.fileno()).st_size
                transfer(TftpContextClientUpload, stream)
                self._publish(SftpProgress(request.request_id, total, total))
        else:
            with local_directory(request.local_path.parent) as directory:
                expected = directory.lstat(request.local_path.name)
                if expected is not None and not request.overwrite:
                    raise OverwriteRequired
                with directory.writer(request.local_path.name, expected) as stream:
                    total = None
                    transfer(TftpContextClientDownload, stream)
                    self._check_cancel(request)
                    self._publish(SftpProgress(request.request_id, stream.tell(), stream.tell()))
        # A late cancellation cannot undo a confirmed remote ACK or a guarded
        # local commit. Report the completed outcome instead of claiming rollback.
        return SftpResult(request.request_id, "ok", "Completed")
