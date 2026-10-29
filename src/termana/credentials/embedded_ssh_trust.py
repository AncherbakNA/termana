"""One bounded, unchanged trust-file snapshot; never accept/repair host keys here."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

import asyncssh

from termana.credentials.ssh_file_metadata import same_ssh_handle_status, same_ssh_path_status
from termana.credentials.windows_ssh_files import (
    WindowsSshFileError,
    open_windows_ssh_file,
    windows_ssh_security_stamp,
)

MAX_EMBEDDED_TRUST_FILE_BYTES: Final = 1024 * 1024


class EmbeddedTrustFileError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("The SSH trust file could not be read safely.")


def load_embedded_trust_file(path: Path) -> asyncssh.SSHKnownHosts:
    """Snapshot and parse off-thread; malformed trust has the same fixed category."""
    try:
        return asyncssh.import_known_hosts(read_embedded_trust_file(path).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise EmbeddedTrustFileError() from None


def read_embedded_trust_file(path: Path) -> bytes:
    """Read before connect, off the worker event loop; empty means no trusted keys."""
    return read_embedded_trust_snapshot(path)[0]


def read_embedded_trust_snapshot(
    path: Path, *, allow_missing: bool = False, max_bytes: int | None = None
) -> tuple[bytes, os.stat_result | None]:
    """Return bytes and metadata from one handle; optionally a proven missing path.

    On Windows even the interactive writer must not lstat before its no-reparse
    open. Only kernel name/path-not-found statuses authorize an empty snapshot.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise EmbeddedTrustFileError()
    limit = MAX_EMBEDDED_TRUST_FILE_BYTES if max_bytes is None else max_bytes
    if type(limit) is not int or not 0 < limit <= 16 * 1024 * 1024:
        raise EmbeddedTrustFileError()
    try:
        selected = None if os.name == "nt" else path.lstat()
        if selected is not None and not stat.S_ISREG(selected.st_mode):
            raise EmbeddedTrustFileError()
        descriptor = (
            open_windows_ssh_file(path)
            if os.name == "nt"
            else os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        )
        try:
            before = os.fstat(descriptor)
            if (
                (selected is not None and not same_ssh_path_status(before, selected))
                or not stat.S_ISREG(before.st_mode)
                or not 0 <= before.st_size <= limit
            ):
                raise EmbeddedTrustFileError()
            if os.name == "posix" and (
                before.st_uid not in (os.getuid(), 0) or stat.S_IMODE(before.st_mode) & 0o022
            ):
                raise EmbeddedTrustFileError()
            security = (
                windows_ssh_security_stamp(descriptor, private=False) if os.name == "nt" else None
            )
            chunks, total = [], 0
            while chunk := os.read(
                descriptor, min(65536, limit + 1 - total)
            ):
                total += len(chunk)
                if total > limit:
                    raise EmbeddedTrustFileError()
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if total != before.st_size or not same_ssh_handle_status(before, after):
                raise EmbeddedTrustFileError()
            if os.name == "nt" and security != windows_ssh_security_stamp(
                descriptor, private=False
            ):
                raise EmbeddedTrustFileError()
            if os.name != "nt" and not same_ssh_path_status(after, path.lstat()):
                raise EmbeddedTrustFileError()
            return b"".join(chunks), after
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        if allow_missing:
            return b"", None
        raise EmbeddedTrustFileError() from None
    except WindowsSshFileError as error:
        if allow_missing and error.native_status in (0xC0000034, 0xC000003A):
            return b"", None
        raise EmbeddedTrustFileError() from None
    except (OSError, ValueError):
        raise EmbeddedTrustFileError() from None
