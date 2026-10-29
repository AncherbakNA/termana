"""Bounded, Qt-neutral atomic writes of captured terminal text."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp
from typing import BinaryIO, Final, Protocol

MAX_TERMINAL_OUTPUT_BYTES: Final = 16 * 1024 * 1024
_ENCODING_CHUNK_CHARACTERS: Final = 16 * 1024
_TEMPORARY_PREFIX: Final = ".termana-terminal-output-"


class TerminalOutputError(RuntimeError):
    """A terminal text snapshot could not be safely saved."""


class _TerminalTextSource(Protocol):
    def iter_lines(self) -> Iterator[str]:
        """Yield physical rows, with ordinary trailing spaces already removed."""
        ...


@dataclass(frozen=True, slots=True, repr=False)
class _TargetIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True, repr=False)
class TerminalOutputTarget:
    """Destination and identity captured immediately after the save dialog.

    Use :func:`prepare_terminal_output_target` to obtain a validated token.
    Its representation deliberately includes neither paths nor terminal text.
    """

    path: Path
    _identity: _TargetIdentity | None
    _parent_device: int
    _parent_inode: int


def prepare_terminal_output_target(path: Path) -> TerminalOutputTarget:
    """Resolve an existing parent without following the final path component."""
    if not isinstance(path, Path):
        raise TerminalOutputError("The terminal output destination must be a path")
    try:
        absolute = Path(os.path.abspath(path))
        parent = absolute.parent.resolve(strict=True)
        parent_stat = parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
            raise TerminalOutputError(
                "The terminal output destination must have a directory parent"
            )
        target = parent / absolute.name
        return TerminalOutputTarget(
            path=target,
            _identity=_target_identity(target),
            _parent_device=parent_stat.st_dev,
            _parent_inode=parent_stat.st_ino,
        )
    except TerminalOutputError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise TerminalOutputError(
            "The terminal output destination directory could not be resolved safely"
        ) from None


def write_terminal_output(target: TerminalOutputTarget, snapshot: _TerminalTextSource) -> int:
    """Write UTF-8/LF text, committing only to the unchanged prepared destination.

    Leading and internal blank rows are retained; trailing blank rows are omitted.
    A nonempty document ends with LF, and an all-blank document is an empty file.
    At most ``MAX_TERMINAL_OUTPUT_BYTES`` bytes can be published. Encoding and
    writing proceed in bounded chunks, without constructing a second full copy.
    """
    if not isinstance(target, TerminalOutputTarget):
        raise TerminalOutputError("The terminal output destination was not prepared")
    temporary_path: Path | None = None
    descriptor: int | None = None
    cleanup_directory: int | None = None
    cleanup_anchored = False
    try:
        _require_unchanged(target)
        # Keeping a directory handle also lets cleanup find the temporary file
        # if the selected directory is renamed while the snapshot is encoded.
        if os.unlink in os.supports_dir_fd:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            cleanup_directory = os.open(target.path.parent, flags)
            opened = os.fstat(cleanup_directory)
            if (opened.st_dev, opened.st_ino) != (target._parent_device, target._parent_inode):
                raise TerminalOutputError("The terminal output destination directory changed")

        descriptor, temporary_name = mkstemp(
            prefix=_TEMPORARY_PREFIX, suffix=".tmp", dir=target.path.parent
        )
        temporary_path = Path(temporary_name)
        if cleanup_directory is not None:
            try:
                anchored = os.stat(
                    temporary_path.name, dir_fd=cleanup_directory, follow_symlinks=False
                )
                opened = os.fstat(descriptor)
                cleanup_anchored = (anchored.st_dev, anchored.st_ino) == (
                    opened.st_dev,
                    opened.st_ino,
                )
            except FileNotFoundError:
                pass
        _require_unchanged(target)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as temporary_file:
            descriptor = None
            byte_count = _write_lines(temporary_file, snapshot)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        _require_unchanged(target)
        os.replace(temporary_path, target.path)
        temporary_path = None
        return byte_count
    except TerminalOutputError:
        raise
    except Exception:
        # Iterator/encoding errors can include the captured text. Do not expose
        # their arguments, representation, or chained traceback to the UI.
        raise TerminalOutputError("Could not write the terminal output file") from None
    finally:
        if descriptor is not None:
            _close_best_effort(descriptor)
        if temporary_path is not None:
            try:
                if cleanup_anchored:
                    os.unlink(temporary_path.name, dir_fd=cleanup_directory)
                else:
                    temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        if cleanup_directory is not None:
            _close_best_effort(cleanup_directory)


def _write_lines(destination: BinaryIO, snapshot: _TerminalTextSource) -> int:
    """Delay blank rows until content follows, checking each bounded write."""
    total_bytes = 0
    pending_blank_rows = 0

    def write_chunk(chunk: bytes) -> None:
        nonlocal total_bytes
        if len(chunk) > MAX_TERMINAL_OUTPUT_BYTES - total_bytes:
            raise TerminalOutputError("The terminal output exceeds the 16 MiB size limit")
        destination.write(chunk)
        total_bytes += len(chunk)

    for line in snapshot.iter_lines():
        if not isinstance(line, str) or "\n" in line or "\r" in line:
            raise TerminalOutputError("The terminal output snapshot contains an invalid row")
        if not line:
            pending_blank_rows += 1
            continue
        # Every Unicode character needs at least one UTF-8 byte. This check
        # rejects obviously oversized rows before allocating encoded content.
        if pending_blank_rows + len(line) + 1 > MAX_TERMINAL_OUTPUT_BYTES - total_bytes:
            raise TerminalOutputError("The terminal output exceeds the 16 MiB size limit")
        while pending_blank_rows:
            count = min(pending_blank_rows, _ENCODING_CHUNK_CHARACTERS)
            write_chunk(b"\n" * count)
            pending_blank_rows -= count
        for offset in range(0, len(line), _ENCODING_CHUNK_CHARACTERS):
            write_chunk(line[offset : offset + _ENCODING_CHUNK_CHARACTERS].encode("utf-8"))
        write_chunk(b"\n")
    return total_bytes


def _target_identity(path: Path) -> _TargetIdentity | None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise TerminalOutputError(
            "The terminal output destination could not be inspected"
        ) from None
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise TerminalOutputError(
            "The terminal output destination must be absent or a regular non-symlink file"
        )
    return _TargetIdentity(
        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns
    )


def _require_unchanged(target: TerminalOutputTarget) -> None:
    try:
        parent = target.path.parent.lstat()
    except OSError:
        raise TerminalOutputError("The terminal output destination directory changed") from None
    if (
        stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or (parent.st_dev, parent.st_ino) != (target._parent_device, target._parent_inode)
    ):
        raise TerminalOutputError("The terminal output destination directory changed")
    if _target_identity(target.path) != target._identity:
        raise TerminalOutputError(
            "The terminal output destination changed before it could be saved"
        )


def _close_best_effort(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass
