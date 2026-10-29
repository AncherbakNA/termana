"""Serial SFTP jobs on an already verified SSH connection, never on the GUI thread."""

import asyncio
import errno
import hashlib
import os
import posixpath
import stat
import tempfile
from contextlib import aclosing, asynccontextmanager
from io import BytesIO
from pathlib import Path

import asyncssh

from termana.credentials.ssh_file_metadata import same_ssh_handle_status
from termana.models.sftp import (
    MAX_SFTP_EDIT_BYTES,
    MAX_SFTP_ENTRIES,
    MAX_SFTP_LISTING_NAME_BYTES,
    MAX_SFTP_PATH_CHARACTERS,
    MAX_SFTP_PENDING,
    SFTP_CHUNK_BYTES,
    SftpEntry,
    SftpFileVersion,
    SftpOperation,
    SftpProgress,
    SftpResult,
)
from termana.terminal.sftp_local_files import SftpSourceChangedError, open_sftp_source
from termana.terminal.sftp_tree import SftpTreeTransfer, TreeConflictRequired, TreePlanError
from termana.terminal.sftp_tree_local import TreeLocalChangedError

SFTP_IO_TIMEOUT = 30
SFTP_CLEANUP_TIMEOUT = 3
_STORAGE_ERRNOS = frozenset((errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)))


class _Cancelled(Exception):
    pass


class _OverwriteRequired(Exception):
    pass


class _SftpUnavailable(Exception):
    pass


class _AlreadyExists(Exception):
    pass


class _CleanupUnconfirmed(Exception):
    pass


class _FileCloseUnconfirmed(Exception):
    pass


class _DestinationChanged(Exception):
    pass


class _RemoteEditChanged(Exception):
    pass


def _error_result(request, error):
    """Describe the operation, never interpolate untrusted exception text or paths."""
    action = request.operation.label
    if isinstance(error, _SftpUnavailable):
        reason = "The server does not provide an SFTP file channel; SSH stays open"
    elif isinstance(error, TreePlanError):
        reason = str(error)
    elif isinstance(error, TreeLocalChangedError):
        reason = "A local folder or destination changed; choose it again"
    elif isinstance(error, SftpSourceChangedError):
        reason = "The local source changed or is not a supported regular file; select it again"
    elif isinstance(error, _DestinationChanged):
        reason = "The local destination changed; select it again before replacing it"
    elif isinstance(error, _RemoteEditChanged):
        reason = "The remote file changed; keep the local edit and reopen the remote file"
    elif isinstance(error, _CleanupUnconfirmed):
        reason = "Temporary file cleanup could not be confirmed"
    elif isinstance(error, _FileCloseUnconfirmed):
        reason = "The file handle close could not be confirmed"
    elif isinstance(error, (_AlreadyExists, asyncssh.SFTPFileAlreadyExists, FileExistsError)):
        reason = "The destination already exists; choose another name"
    elif isinstance(error, (asyncssh.SFTPPermissionDenied, PermissionError)):
        reason = "Permission denied; check access to the source and destination"
    elif isinstance(error, (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath, FileNotFoundError)):
        reason = "File or folder no longer exists; refresh and check the path"
    elif isinstance(error, (asyncssh.SFTPNotADirectory, NotADirectoryError)):
        reason = "The selected path is not a folder"
    elif isinstance(error, (asyncssh.SFTPNoSpaceOnFilesystem, asyncssh.SFTPQuotaExceeded)) or (
        isinstance(error, OSError) and error.errno in _STORAGE_ERRNOS
    ):
        reason = "Storage space or quota is exhausted"
    elif isinstance(error, (asyncssh.SFTPConnectionLost, asyncssh.SFTPNoConnection)):
        reason = "The file channel was disconnected"
    elif isinstance(error, asyncssh.SFTPOpUnsupported):
        reason = "The server does not support this operation"
    elif isinstance(error, TimeoutError):
        reason = "The operation timed out"
    elif isinstance(error, asyncio.CancelledError):
        reason = "SSH ended during this operation"
    else:
        reason = "The operation failed"
    message = f"{action}: {reason}"
    if request.operation is not SftpOperation.LIST:
        message += "; check destination state before retrying"
    return SftpResult(request.request_id, "error", message)


class SftpRuntime:
    """The backend admission gate bounds jobs and unread results to 16 total."""

    def __init__(self, owned, publish):
        self._owned = owned
        self._publish = publish
        self._queue = asyncio.Queue(maxsize=MAX_SFTP_PENDING)
        self._cancelled = set()
        self._accepted = set()
        self._client = None
        self._closed = False
        self._tree_transfer = None

    def enqueue(self, request):
        if self._closed:
            self._publish(
                SftpResult(request.request_id, "cancelled", "SSH ended before this job started")
            )
            return
        self._queue.put_nowait(request)
        self._accepted.add(request.request_id)

    def cancel(self, request_id):
        if request_id not in self._accepted:
            return
        self._cancelled.add(request_id)
        # Let the current bounded I/O finish, then cancel before the next chunk.
        # Cancelling an exclusive remote open midway can orphan an unknown handle.

    def _check_cancel(self, request):
        if request.request_id in self._cancelled:
            raise _Cancelled

    async def _io(self, awaitable):
        return await asyncio.wait_for(awaitable, SFTP_IO_TIMEOUT)

    async def _perform(self, request):
        self._check_cancel(request)
        if self._client is None:
            try:
                self._client = await self._io(self._owned.open_sftp())
            except asyncssh.ChannelOpenError:
                raise _SftpUnavailable from None
        if request.operation.is_tree:
            self._tree_transfer = SftpTreeTransfer(self, request)
            return await self._tree_transfer.run()
        if request.operation is SftpOperation.LIST:
            return await self._list(request)
        if request.operation is SftpOperation.MKDIR:
            if await self._remote_exists(request.remote_path) is not None:
                raise _AlreadyExists
            self._check_cancel(request)
            await self._io(
                self._client.mkdir(request.remote_path, attrs=asyncssh.SFTPAttrs(permissions=0o700))
            )
            # Once the server acknowledges creation, a late Cancel must not
            # claim that the new directory was removed or rolled back.
            return SftpResult(request.request_id, "ok", "Folder created")
        if request.operation is SftpOperation.RENAME:
            attrs = await self._remote_exists(request.remote_path)
            if attrs is None or not stat.S_ISREG(attrs.permissions or 0):
                raise ValueError("Rename supports existing regular files only")
            if await self._remote_exists(request.target_path) is not None:
                raise _AlreadyExists
            self._check_cancel(request)
            # Default flags=0 is the library's no-overwrite operation. Never
            # fall back to posix_rename or delete the destination.
            await self._io(self._client.rename(request.remote_path, request.target_path))
            return SftpResult(request.request_id, "ok", "File renamed/moved")
        version = await self._transfer(request)
        return SftpResult(request.request_id, "ok", "Completed", version=version)

    async def run(self):
        try:
            while True:
                request = await self._queue.get()
                self._tree_transfer = None
                try:
                    result = await self._perform(request)
                except _Cancelled:
                    result = SftpResult(request.request_id, "cancelled", "Cancelled")
                except TreeConflictRequired:
                    result = SftpResult(
                        request.request_id, "overwrite-required", "Destination exists"
                    )
                except _OverwriteRequired:
                    result = (
                        _error_result(
                            request,
                            TreePlanError(
                                "A destination appeared during copying; review the partial folder before retrying"
                            ),
                        )
                        if self._tree_transfer is not None
                        else SftpResult(
                            request.request_id, "overwrite-required", "Destination exists"
                        )
                    )
                except asyncio.CancelledError as error:
                    self._publish(self._tree_result(_error_result(request, error)))
                    raise
                except Exception as error:
                    result = _error_result(request, error)
                    if isinstance(
                        error,
                        (
                            asyncssh.SFTPConnectionLost,
                            asyncssh.SFTPNoConnection,
                            TimeoutError,
                            _CleanupUnconfirmed,
                            _FileCloseUnconfirmed,
                        ),
                    ):
                        # Never replay the failed request. Detach this file
                        # channel; the next admitted job opens a fresh channel
                        # on the same already-verified SSH connection.
                        await self._drop_client()
                self._publish(self._tree_result(result))
                self._tree_transfer = None
                self._cancelled.discard(request.request_id)
                self._accepted.discard(request.request_id)
        finally:
            await self.close()

    def _tree_result(self, result):
        return self._tree_transfer.result(result) if self._tree_transfer is not None else result

    async def close(self):
        self._closed = True
        while not self._queue.empty():
            request = self._queue.get_nowait()
            self._publish(
                SftpResult(request.request_id, "cancelled", "SSH ended before this job started")
            )
        self._cancelled.clear()
        self._accepted.clear()
        await self._drop_client()

    async def _drop_client(self):
        if self._client is not None:
            client, self._client = self._client, None
            try:
                client.exit()
                await asyncio.wait_for(client.wait_closed(), SFTP_CLEANUP_TIMEOUT)
            except (TimeoutError, OSError, asyncssh.Error):
                pass  # Full SSH shutdown also retires any remaining channels.

    @asynccontextmanager
    async def _file(self, path, mode):
        options = {"attrs": asyncssh.SFTPAttrs(permissions=0o600)} if mode == "xb" else {}
        remote = await self._io(self._client.open(path, mode, encoding=None, **options))
        try:
            yield remote
        finally:
            try:
                await asyncio.wait_for(remote.close(), SFTP_CLEANUP_TIMEOUT)
            except TimeoutError:
                raise _FileCloseUnconfirmed from None

    async def _list(self, request):
        directory = await self._io(self._client.realpath(request.remote_path))
        if (
            not isinstance(directory, str)
            or not posixpath.isabs(directory)
            or len(directory) > MAX_SFTP_PATH_CHARACTERS
            or "\0" in directory
        ):
            raise ValueError("Unsupported canonical remote directory")
        try:
            directory.encode("utf-8")
        except UnicodeError:
            raise ValueError("Unsupported canonical remote directory") from None
        entries = []
        truncated = False
        scanned = 0
        omitted = 0
        name_bytes = 0
        async with aclosing(self._client.scandir(directory)) as iterator:
            while True:
                self._check_cancel(request)
                try:
                    entry = await self._io(anext(iterator))
                except StopAsyncIteration:
                    break
                # Count all server records, not only accepted filenames. An
                # endless stream of unsafe names must not bypass the listing cap.
                if scanned == MAX_SFTP_ENTRIES:
                    truncated = True
                    break
                scanned += 1
                name = entry.filename
                if name in (".", ".."):
                    continue
                if (
                    not isinstance(name, str)
                    or not name
                    or "/" in name
                    or "\0" in name
                    or len(name) > MAX_SFTP_PATH_CHARACTERS
                    or len(posixpath.join(directory, name)) > MAX_SFTP_PATH_CHARACTERS
                ):
                    omitted += 1
                    continue
                try:
                    encoded_length = len(name.encode("utf-8"))
                except UnicodeError:
                    omitted += 1
                    continue
                if name_bytes + encoded_length > MAX_SFTP_LISTING_NAME_BYTES:
                    truncated = True
                    break
                name_bytes += encoded_length
                # Optional metadata is untrusted. Unknown is not zero/epoch;
                # omit malformed fields rather than allowing UI arithmetic errors.
                permissions = entry.attrs.permissions
                permissions = (
                    permissions
                    if type(permissions) is int and 0 <= permissions <= 0xFFFFFFFF
                    else None
                )
                mode = permissions or 0
                kind = (
                    "directory"
                    if stat.S_ISDIR(mode)
                    else "file"
                    if stat.S_ISREG(mode)
                    else "link"
                    if stat.S_ISLNK(mode)
                    else "other"
                )
                size = entry.attrs.size
                size = size if type(size) is int and 0 <= size <= 0xFFFFFFFFFFFFFFFF else None
                modified = getattr(entry.attrs, "mtime", None)
                modified = (
                    modified
                    if type(modified) is int and 0 <= modified <= 0x7FFFFFFFFFFFFFFF
                    else None
                )
                entries.append(SftpEntry(name, kind, size, permissions, modified))
        entries.sort(key=lambda entry: (entry.kind != "directory", entry.name.casefold()))
        return SftpResult(
            request.request_id,
            "ok",
            directory=directory,
            entries=tuple(entries),
            truncated=truncated,
            omitted_entries=omitted,
        )

    async def _remote_exists(self, path):
        try:
            return await self._io(self._client.lstat(path))
        except asyncssh.SFTPNoSuchFile:
            return None

    async def _transfer(self, request):
        if request.operation in (SftpOperation.DOWNLOAD, SftpOperation.EDIT_DOWNLOAD):
            return await self._download(request)
        else:
            return await self._upload(request)

    async def _download(self, request):
        target = request.local_path
        attrs = await self._io(self._client.lstat(request.remote_path))
        if not stat.S_ISREG(attrs.permissions or 0):
            raise ValueError("Only regular files can be downloaded")
        editing = request.operation is SftpOperation.EDIT_DOWNLOAD
        if editing and (attrs.size is None or attrs.size > MAX_SFTP_EDIT_BYTES):
            raise ValueError("Text editing is limited to 2 MiB")
        windows_editing = editing and os.name == "nt"
        edit_target = None
        if windows_editing:
            from termana.terminal.windows_sftp_edit_download import WindowsEditDownloadTarget

            edit_target = request.edit_download_target
            if (
                type(edit_target) is not WindowsEditDownloadTarget
                or request.overwrite
                or not edit_target.matches(target)
            ):
                raise ValueError("An exact private edited-copy target is required")
        digest = hashlib.sha256() if editing else None
        target_status = None
        if not windows_editing:
            try:
                target_status = target.lstat()
            except FileNotFoundError:
                target_status = None
        target_mode = target_status.st_mode if target_status is not None else None
        if target_mode is not None and not stat.S_ISREG(target_mode):
            raise ValueError("Destination must be a regular file")
        if target_status is not None and not request.overwrite:
            raise _OverwriteRequired
        staged = None
        try:
            with (
                BytesIO()
                if windows_editing
                else tempfile.NamedTemporaryFile(
                    prefix=".termana-", suffix=".part", dir=target.parent, delete=False
                )
            ) as local:
                if not windows_editing:
                    staged = Path(local.name)
                async with self._file(request.remote_path, "rb") as remote:
                    size = (await self._io(remote.stat())).size
                    transferred = 0
                    while True:
                        self._check_cancel(request)
                        chunk = await self._io(remote.read(SFTP_CHUNK_BYTES))
                        if not chunk:
                            break
                        if editing and transferred + len(chunk) > MAX_SFTP_EDIT_BYTES:
                            raise ValueError("Text editing is limited to 2 MiB")
                        local.write(chunk)
                        transferred += len(chunk)
                        if editing:
                            digest.update(chunk)
                        self._publish(SftpProgress(request.request_id, transferred, size))
                if windows_editing:
                    data = local.getvalue()
            self._check_cancel(request)
            if windows_editing:
                # Remote close and the final cancellation check precede any
                # local create. The synchronous owner capability cannot be
                # replaced with an unowned path or an ordinary download target.
                edit_target.publish(data)
                return SftpFileVersion(transferred, digest.digest())
            if request.overwrite:
                try:
                    current_status = target.lstat()
                except FileNotFoundError:
                    current_status = None
                if (target_status is None) != (current_status is None) or (
                    target_status is not None
                    and not same_ssh_handle_status(target_status, current_status)
                ):
                    raise _DestinationChanged
            if request.overwrite and target_mode is not None:
                os.chmod(staged, stat.S_IMODE(target_mode) & 0o777)
            # No await during local commit: exclusive hard link prevents an
            # intervening file from being overwritten without consent.
            if request.overwrite:
                os.replace(staged, target)
            else:
                try:
                    os.link(staged, target)
                except FileExistsError:
                    raise _OverwriteRequired from None
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        if editing:
            return SftpFileVersion(transferred, digest.digest())

    async def _edit_version(self, request):
        attrs = await self._remote_exists(request.remote_path)
        if (
            attrs is None
            or not stat.S_ISREG(attrs.permissions or 0)
            or attrs.size is None
            or attrs.size > MAX_SFTP_EDIT_BYTES
        ):
            raise _RemoteEditChanged
        digest, size = hashlib.sha256(), 0
        async with self._file(request.remote_path, "rb") as remote:
            while True:
                self._check_cancel(request)
                chunk = await self._io(remote.read(SFTP_CHUNK_BYTES))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SFTP_EDIT_BYTES:
                    raise _RemoteEditChanged
                digest.update(chunk)
        return SftpFileVersion(size, digest.digest())

    async def _upload(self, request, *, open_source=None, progress=None, before_commit=None):
        attrs = await self._remote_exists(request.remote_path)
        if attrs is not None:
            if not stat.S_ISREG(attrs.permissions or 0):
                raise ValueError("Destination must be a regular file")
            if not request.overwrite:
                raise _OverwriteRequired
        staged = posixpath.join(
            posixpath.dirname(request.remote_path), f".termana-{request.request_id.hex}.part"
        )
        created = False
        editing = request.operation is SftpOperation.EDIT_UPLOAD
        digest = hashlib.sha256() if editing else None
        try:
            with (open_source or open_sftp_source)(request.local_path) as local:
                size = os.fstat(local.fileno()).st_size
                if request.operation is SftpOperation.EDIT_UPLOAD and size > MAX_SFTP_EDIT_BYTES:
                    raise ValueError("Edited text is too large")
                async with self._file(staged, "xb") as remote:
                    created = True
                    transferred = 0
                    while chunk := local.read(SFTP_CHUNK_BYTES):
                        self._check_cancel(request)
                        await self._io(remote.write(chunk))
                        transferred += len(chunk)
                        if editing:
                            if transferred > MAX_SFTP_EDIT_BYTES:
                                raise ValueError("Edited text is too large")
                            digest.update(chunk)
                        (progress or self._publish)(
                            SftpProgress(request.request_id, transferred, size)
                        )
                    if attrs is not None and request.overwrite:
                        # Preserve ordinary permission bits, but not privileged
                        # setuid/setgid bits, owners or server-specific ACLs.
                        await self._io(
                            remote.setstat(
                                asyncssh.SFTPAttrs(permissions=(attrs.permissions or 0) & 0o777)
                            )
                        )
            self._check_cancel(request)
            if request.operation is SftpOperation.EDIT_UPLOAD:
                if await self._edit_version(request) != request.version:
                    raise _RemoteEditChanged
            if before_commit is not None:
                await before_commit()
                self._check_cancel(request)
            # Cancellation is cooperative. Once commit starts, finish and report
            # its outcome, not a misleading "cancelled" after a successful rename.
            if request.overwrite:
                await self._io(self._client.posix_rename(staged, request.remote_path))
            else:
                try:
                    await self._io(self._client.rename(staged, request.remote_path))
                except (asyncssh.SFTPFileAlreadyExists, asyncssh.SFTPFailure):
                    if await self._remote_exists(request.remote_path) is not None:
                        raise _OverwriteRequired from None
                    raise
            created = False
        finally:
            if created:
                try:
                    await asyncio.wait_for(self._client.remove(staged), SFTP_CLEANUP_TIMEOUT)
                except Exception:
                    # Do not call an incomplete cleanup "cancelled/completed".
                    raise _CleanupUnconfirmed from None
        if editing:
            return SftpFileVersion(transferred, digest.digest())
