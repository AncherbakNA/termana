"""Bounded, Qt-neutral file I/O for portable connection transfers."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp
from typing import Final

from termana.storage.connection_transfer import MAX_CONNECTION_TRANSFER_BYTES

MAX_CONNECTION_TRANSFER_FILE_BYTES: Final = MAX_CONNECTION_TRANSFER_BYTES
_READ_CHUNK_BYTES: Final = 64 * 1024
_TEMPORARY_FILE_MODE: Final = 0o600


class ConnectionTransferIOError(RuntimeError):
    """Raised when a transfer file cannot be read or committed safely."""


class ConnectionTransferPathError(ConnectionTransferIOError):
    """Raised when a selected transfer path is unsafe or conflicts with app data."""


class ConnectionTransferSizeError(ConnectionTransferIOError):
    """Raised when transfer bytes exceed the public portable-file boundary."""


@dataclass(frozen=True, slots=True)
class _ExistingTargetSnapshot:
    """Identity and mutation fields checked again immediately before replacement."""

    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True, init=False)
class PreparedConnectionExportTarget:
    """Opaque destination identity captured immediately after the save dialog."""

    path: Path
    _connections_path: Path | None
    _target_snapshot: _ExistingTargetSnapshot | None
    _parent_device: int
    _parent_inode: int

    @classmethod
    def _create(
        cls,
        *,
        path: Path,
        connections_path: Path | None,
        target_snapshot: _ExistingTargetSnapshot | None,
        parent_device: int,
        parent_inode: int,
    ) -> PreparedConnectionExportTarget:
        """Build one validated token only from the module's preparation function."""
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("Prepared export path must be absolute")
        if connections_path is not None and (
            not isinstance(connections_path, Path) or not connections_path.is_absolute()
        ):
            raise TypeError("Prepared connections path must be a Path or None")
        if target_snapshot is not None and not isinstance(target_snapshot, _ExistingTargetSnapshot):
            raise TypeError("Prepared target snapshot is invalid")
        for field_name, value in (
            ("parent_device", parent_device),
            ("parent_inode", parent_inode),
        ):
            if type(value) is not int or value < 0:
                raise TypeError(f"{field_name} must be an int")
        instance = object.__new__(cls)
        object.__setattr__(instance, "path", path)
        object.__setattr__(instance, "_connections_path", connections_path)
        object.__setattr__(instance, "_target_snapshot", target_snapshot)
        object.__setattr__(instance, "_parent_device", parent_device)
        object.__setattr__(instance, "_parent_inode", parent_inode)
        return instance


def validate_connection_export_target(
    target_path: Path,
    connections_path: Path | None,
) -> Path:
    """Return an absolute write path that cannot name the live connections file.

    Comparison resolves aliases only in temporary comparison values. The returned
    path retains its final component so a caller never follows a final symlink when
    writing. :func:`write_connection_transfer_bytes` performs the same guard again
    immediately before its atomic replacement.
    """
    target = _absolute_path(target_path, field_name="Export target")
    if connections_path is None:
        return target
    connections = _absolute_path(connections_path, field_name="Connections file")

    try:
        same_existing_file = os.path.samefile(target, connections)
    except FileNotFoundError:
        same_existing_file = False
    except OSError as error:
        raise ConnectionTransferPathError(
            "The export target could not be compared with the live connections file"
        ) from error

    if same_existing_file or _resolved_comparison_path(target) == _resolved_comparison_path(
        connections
    ):
        raise ConnectionTransferPathError(
            "The live connections file cannot be used as an export target"
        )
    return target


def prepare_connection_export_target(
    target_path: Path,
    connections_path: Path | None,
) -> PreparedConnectionExportTarget:
    """Capture the exact destination state immediately after user selection."""
    target = validate_connection_export_target(target_path, connections_path)
    try:
        resolved_parent = target.parent.resolve(strict=True)
        parent_stat = resolved_parent.lstat()
    except (OSError, RuntimeError) as error:
        raise ConnectionTransferPathError(
            "The export destination directory could not be resolved safely"
        ) from error
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ConnectionTransferPathError(
            "The export destination parent must be a regular directory"
        )
    anchored_target = resolved_parent / target.name
    anchored_connections = (
        None
        if connections_path is None
        else _absolute_path(connections_path, field_name="Connections file")
    )
    validate_connection_export_target(anchored_target, anchored_connections)
    return PreparedConnectionExportTarget._create(
        path=anchored_target,
        connections_path=anchored_connections,
        target_snapshot=_snapshot_export_target(anchored_target),
        parent_device=parent_stat.st_dev,
        parent_inode=parent_stat.st_ino,
    )


def read_connection_transfer_bytes(
    path: Path,
    *,
    max_bytes: int = MAX_CONNECTION_TRANSFER_FILE_BYTES,
) -> bytes:
    """Read one regular, non-symlink file without ever exceeding ``max_bytes``."""
    limit = _validated_max_bytes(max_bytes)
    source = _absolute_path(path, field_name="Import source")
    path_stat = _lstat_regular_file(
        source,
        missing_message="The import file does not exist",
        unsafe_message="The import source must be a regular non-symlink file",
    )
    if path_stat.st_size > limit:
        raise ConnectionTransferSizeError("The connection transfer file is too large")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(source, flags)
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ConnectionTransferPathError(
                "The import source must be a regular non-symlink file"
            )
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise ConnectionTransferPathError(
                "The import source changed before it could be opened safely"
            )
        if opened_stat.st_size > limit:
            raise ConnectionTransferSizeError("The connection transfer file is too large")

        with os.fdopen(descriptor, "rb", closefd=True) as source_file:
            descriptor = None
            chunks: list[bytes] = []
            total_bytes = 0
            while True:
                chunk = source_file.read(min(_READ_CHUNK_BYTES, limit + 1 - total_bytes))
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > limit:
                    raise ConnectionTransferSizeError("The connection transfer file is too large")

            completed_stat = os.fstat(source_file.fileno())
            if (
                completed_stat.st_size != opened_stat.st_size
                or completed_stat.st_mtime_ns != opened_stat.st_mtime_ns
                or completed_stat.st_ctime_ns != opened_stat.st_ctime_ns
            ):
                raise ConnectionTransferIOError("The import file changed while it was being read")
        return b"".join(chunks)
    except (ConnectionTransferIOError, ConnectionTransferPathError):
        raise
    except OSError as error:
        raise ConnectionTransferIOError("Could not read the connection transfer file") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def write_connection_transfer_bytes(
    target_path: Path,
    payload: bytes,
    *,
    connections_path: Path | None,
    max_bytes: int = MAX_CONNECTION_TRANSFER_FILE_BYTES,
) -> None:
    """Prepare and atomically write a target within one direct function call."""
    prepared = prepare_connection_export_target(target_path, connections_path)
    write_prepared_connection_transfer_bytes(
        prepared,
        payload,
        max_bytes=max_bytes,
    )


def write_prepared_connection_transfer_bytes(
    prepared: PreparedConnectionExportTarget,
    payload: bytes,
    *,
    max_bytes: int = MAX_CONNECTION_TRANSFER_FILE_BYTES,
) -> None:
    """Commit only if the destination still matches its post-dialog snapshot."""
    if not isinstance(prepared, PreparedConnectionExportTarget):
        raise TypeError("prepared must be a PreparedConnectionExportTarget")
    limit = _validated_max_bytes(max_bytes)
    if type(payload) is not bytes:
        raise TypeError("Connection transfer payload must be bytes")
    if len(payload) > limit:
        raise ConnectionTransferSizeError("The connection transfer payload is too large")

    target = prepared.path
    _require_prepared_parent_unchanged(prepared)
    _require_export_target_unchanged(target, prepared._target_snapshot)
    validate_connection_export_target(target, prepared._connections_path)
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = mkstemp(
            prefix=".termana-connection-export-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, _TEMPORARY_FILE_MODE)
        with os.fdopen(descriptor, "wb", closefd=True) as temporary_file:
            descriptor = None
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        _require_prepared_parent_unchanged(prepared)
        _require_export_target_unchanged(target, prepared._target_snapshot)
        validate_connection_export_target(target, prepared._connections_path)
        os.replace(temporary_path, target)
        temporary_path = None
    except (ConnectionTransferIOError, ConnectionTransferPathError):
        raise
    except OSError as error:
        raise ConnectionTransferIOError("Could not write the connection transfer file") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    _fsync_directory_best_effort(target.parent)


def _absolute_path(value: Path, *, field_name: str) -> Path:
    """Normalize lexical aliases without resolving a final path component."""
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a Path")
    return Path(os.path.abspath(value))


def _resolved_comparison_path(path: Path) -> str:
    """Resolve existing aliases into a normalized value used only for comparison."""
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ConnectionTransferPathError(
            "The export target contains an unsafe path alias"
        ) from error
    return os.path.normcase(os.fspath(resolved))


def _validated_max_bytes(value: int) -> int:
    """Allow callers to tighten, but never loosen, the public file-size limit."""
    if type(value) is not int:
        raise TypeError("max_bytes must be an int")
    if not 1 <= value <= MAX_CONNECTION_TRANSFER_FILE_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_CONNECTION_TRANSFER_FILE_BYTES}")
    return value


def _lstat_regular_file(
    path: Path,
    *,
    missing_message: str,
    unsafe_message: str,
) -> os.stat_result:
    """Inspect a final path component without following a symbolic link."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError as error:
        raise ConnectionTransferPathError(missing_message) from error
    except OSError as error:
        raise ConnectionTransferPathError("The selected path could not be inspected") from error
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise ConnectionTransferPathError(unsafe_message)
    return path_stat


def _snapshot_export_target(target: Path) -> _ExistingTargetSnapshot | None:
    """Capture an existing regular target or represent an exact absent destination."""
    try:
        path_stat = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConnectionTransferPathError("The export target could not be inspected") from error
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise ConnectionTransferPathError(
            "The export target must be absent or a regular non-symlink file"
        )
    return _ExistingTargetSnapshot(
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
        size=path_stat.st_size,
        modified_ns=path_stat.st_mtime_ns,
        changed_ns=path_stat.st_ctime_ns,
    )


def _require_prepared_parent_unchanged(
    prepared: PreparedConnectionExportTarget,
) -> None:
    """Reject a destination directory replaced after the save dialog returned."""
    try:
        parent_stat = prepared.path.parent.lstat()
    except OSError as error:
        raise ConnectionTransferPathError(
            "The export destination directory changed before writing"
        ) from error
    if (
        stat.S_ISLNK(parent_stat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or (parent_stat.st_dev, parent_stat.st_ino)
        != (prepared._parent_device, prepared._parent_inode)
    ):
        raise ConnectionTransferPathError("The export destination directory changed before writing")


def _require_export_target_unchanged(
    target: Path,
    expected: _ExistingTargetSnapshot | None,
) -> None:
    """Refuse to overwrite a destination changed after the save dialog returned."""
    current = _snapshot_export_target(target)
    if current != expected:
        raise ConnectionTransferPathError(
            "The export target changed before it could be replaced safely"
        )


def _fsync_directory_best_effort(directory: Path) -> None:
    """Persist the atomic rename on platforms that expose directory descriptors."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, flags)
        os.fsync(descriptor)
    except OSError:
        # Windows and some filesystems cannot open or sync directory handles.
        pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
