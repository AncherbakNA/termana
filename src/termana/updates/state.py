"""Opt-in durable updater checkpoints, isolated from app settings and secrets.

Callers explicitly provide an owned directory with an existing parent. A lock
left by a crashed process is NOT silently stolen; recovery requires review.
"""

import hashlib
import json
import os
import stat
from pathlib import Path

from termana.storage.atomic_file import replace_file_atomically
from termana.storage.json_objects import reject_duplicate_json_keys
from termana.updates.client import UpdateCheckpoint
from termana.updates.manifest import ReleaseRepository, UpdateError

_LIMIT = 64 * 1024


class UpdateStateStore:
    """Serialize cooperating writers; never recover malformed state as empty."""

    def __init__(self, directory: Path):
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise UpdateError("Updater state directory must be absolute")
        self.directory = directory
        self.path = directory / "checkpoints.json"
        self.lock = directory / ".checkpoint.lock"

    def _guard(self):
        try:
            for parent in (*reversed(self.directory.parents), self.directory):
                if parent.is_symlink():
                    raise UpdateError("Updater state path must not contain symlinks")
            if os.name == "nt":
                self._guard_windows_directory()
            else:
                self.directory.mkdir(mode=0o700, exist_ok=True)
            info = self.directory.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise UpdateError("Updater state location is not a directory")
            if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise UpdateError("Updater state directory must be private to its owner")
            if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
                raise UpdateError("Unsafe updater checkpoint file")
        except OSError:
            raise UpdateError("Cannot access updater state directory") from None

    def _guard_windows_directory(self):
        # Reuse the existing local-NTFS owner/DACL/no-reparse policy. Never
        # interpret mkdir(mode=0700) as a Windows ACL or repair an existing ACL.
        from termana.credentials.windows_private_files import (
            create_windows_parent,
            private_windows_directory,
        )
        from termana.credentials.windows_ssh_files import WindowsSshFileError

        try:
            try:
                with private_windows_directory(self.directory, private=True):
                    pass
            except FileNotFoundError:
                create_windows_parent(self.path)
                with private_windows_directory(self.directory, private=True):
                    pass
        except (OSError, WindowsSshFileError):
            raise UpdateError("A private local updater directory could not be confirmed") from None

    def _open_checkpoint(self):
        if os.name != "nt":
            return os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        from termana.credentials.windows_ssh_files import (
            WindowsSshFileError,
            open_windows_ssh_file,
        )

        try:
            return open_windows_ssh_file(self.path)
        except WindowsSshFileError as error:
            if error.native_status in (0xC0000034, 0xC000003A):
                raise FileNotFoundError() from None
            raise UpdateError("Unsafe updater checkpoint file") from None

    @staticmethod
    def _file_security(descriptor):
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UpdateError("Unsafe updater checkpoint file")
        if os.name != "nt":
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise UpdateError("Updater checkpoint must be private to its owner")
            return info.st_uid, info.st_mode
        from termana.credentials.windows_ssh_files import (
            WindowsSshFileError,
            windows_ssh_security_stamp,
        )

        try:
            return windows_ssh_security_stamp(descriptor, private=True)
        except WindowsSshFileError:
            raise UpdateError("Unsafe updater checkpoint permissions") from None

    def _read(self):
        self._guard()
        try:
            try:
                descriptor = self._open_checkpoint()
            except FileNotFoundError:
                return []
            with os.fdopen(descriptor, "rb") as stream:
                security = self._file_security(stream.fileno())
                payload = stream.read(_LIMIT + 1)
                if self._file_security(stream.fileno()) != security:
                    raise UpdateError("Updater checkpoint permissions changed")
            if len(payload) > _LIMIT:
                raise UpdateError("Updater state exceeds its limit")
            data = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)
            if (
                type(data) is not dict
                or set(data) != {"version", "entries"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or type(data["entries"]) is not list
                or len(data["entries"]) > 32
            ):
                raise UpdateError("Unsupported updater state format")
            seen = set()
            for row in data["entries"]:
                if type(row) is not dict or set(row) != {
                    "repository",
                    "channel",
                    "sequence",
                    "sha256",
                    "publisher",
                }:
                    raise UpdateError("Invalid updater state entry")
                point = UpdateCheckpoint(
                    ReleaseRepository.parse(row["repository"]),
                    row["channel"],
                    row["sequence"],
                    row["sha256"],
                )
                if (
                    type(row["publisher"]) is not str
                    or len(row["publisher"]) != 64
                    or any(char not in "0123456789abcdef" for char in row["publisher"])
                ):
                    raise UpdateError("Invalid publisher fingerprint")
                identity = (point.repository.identity, point.channel)
                if identity in seen:
                    raise UpdateError("Duplicate updater state identity")
                seen.add(identity)
            return data["entries"]
        except UpdateError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise UpdateError("Updater state is unreadable or invalid; review required") from None

    @staticmethod
    def _publisher(key):
        if type(key) is not bytes or len(key) != 32:
            raise UpdateError("A pinned Ed25519 public key is required")
        return hashlib.sha256(key).hexdigest()

    def load(self, repository, channel, public_key):
        # Validate the namespace even if no file exists.
        UpdateCheckpoint(repository, channel, 1, "0" * 64)
        publisher = self._publisher(public_key)
        for row in self._read():
            if (row["repository"], row["channel"]) == (repository.identity, channel):
                if row["publisher"] != publisher:
                    raise UpdateError("Publisher key changed; explicit rotation required")
                return UpdateCheckpoint(repository, channel, row["sequence"], row["sha256"])
        return None

    def accept(self, checkpoint: UpdateCheckpoint, public_key: bytes):
        """Persist ONLY a checkpoint obtained after successful signed discovery."""
        if type(checkpoint) is not UpdateCheckpoint:
            raise UpdateError("A verified updater checkpoint is required")
        publisher = self._publisher(public_key)
        self._guard()
        try:
            descriptor = os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise UpdateError("Updater state is busy or has an unrecovered lock") from None
        except OSError:
            raise UpdateError("Cannot lock updater state") from None
        lock_info = os.fstat(descriptor)
        os.close(descriptor)
        try:
            rows = self._read()
            old = next(
                (
                    row
                    for row in rows
                    if (row["repository"], row["channel"])
                    == (checkpoint.repository.identity, checkpoint.channel)
                ),
                None,
            )
            if old is not None:
                if old["publisher"] != publisher:
                    raise UpdateError("Publisher key changed; explicit rotation required")
                if checkpoint.sequence < old["sequence"] or (
                    checkpoint.sequence == old["sequence"] and checkpoint.sha256 != old["sha256"]
                ):
                    raise UpdateError("Updater checkpoint rollback or rewrite rejected")
                rows.remove(old)
            if len(rows) >= 32:
                raise UpdateError("Updater checkpoint namespace limit reached")
            rows.append(
                dict(
                    repository=checkpoint.repository.identity,
                    channel=checkpoint.channel,
                    sequence=checkpoint.sequence,
                    sha256=checkpoint.sha256,
                    publisher=publisher,
                )
            )
            payload = json.dumps({"version": 1, "entries": rows}, sort_keys=True).encode()
            replace_file_atomically(self.path, payload)
            if os.name != "nt":
                fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError:
            raise UpdateError("Could not confirm durable updater state") from None
        finally:
            try:
                current = self.lock.lstat()
                if (current.st_dev, current.st_ino) == (lock_info.st_dev, lock_info.st_ino):
                    self.lock.unlink()
            except OSError:
                pass
