"""Explicit host-key consent, separate from the library's signature verification.

The synchronous question runs only in a dedicated SSH worker. Persistence runs
after key exchange, before any credential provider, against an unchanged snapshot.
No protocol, signature or hashed-host matching is implemented here.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

import asyncssh

from termana.credentials.embedded_ssh_trust import (
    EmbeddedTrustFileError,
    read_embedded_trust_snapshot,
)
from termana.credentials.host_trust_records import replace_host_records
from termana.credentials.posix_host_trust import can_create_parent as can_create_posix_parent
from termana.credentials.posix_host_trust import create_parent_after_consent
from termana.credentials.ssh_file_metadata import same_ssh_path_status
from termana.credentials.windows_private_files import (
    can_create_windows_parent,
    commit_windows_trust,
    windows_trust_save_supported,
)
from termana.credentials.windows_ssh_files import (
    WindowsSshFileError,
)
from termana.models.ssh_credential import SshEndpoint

_WRITE_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class EmbeddedHostKeyQuestion:
    endpoint: SshEndpoint
    algorithm: str
    fingerprint: str
    changed: bool
    previous_fingerprints: tuple[str, ...]
    will_save: bool
    creates_directory: bool = False

    @property
    def display_endpoint(self) -> str:
        host = self.endpoint.host
        return f"[{host}]:{self.endpoint.port}" if ":" in host else f"{host}:{self.endpoint.port}"

    @property
    def prompt(self) -> str:
        title = "Server identity changed" if self.changed else "First connection to this server"
        previous = ""
        if self.changed:
            previous = "\nPreviously trusted: " + ", ".join(self.previous_fingerprints)
        action = (
            "Accept and save this key after signature verification"
            if self.will_save
            else "Accept this key for this session only (the trust file will not be changed)"
        )
        return (
            f"{title}: {self.display_endpoint}{previous}\n"
            f"Presented {self.algorithm} fingerprint: {self.fingerprint}\n"
            "Verify this fingerprint through a trusted channel. If the change is unexpected, "
            f"answer no.\n"
            + (
                "A private SSH folder will be created only if you accept.\n"
                if self.creates_directory
                else ""
            )
            + f"{action}? (yes/no)"
        )


type EmbeddedHostKeyProvider = Callable[[EmbeddedHostKeyQuestion], bool]


@dataclass(frozen=True)
class _Snapshot:
    data: bytes
    status: os.stat_result | None


def _snapshot(path: Path) -> _Snapshot:
    data, status = read_embedded_trust_snapshot(path, allow_missing=True)
    return _Snapshot(data, status)


def _unchanged(path: Path, expected: _Snapshot) -> None:
    now = _snapshot(path)
    if now.data != expected.data or (now.status is None) != (expected.status is None):
        raise EmbeddedTrustFileError()
    if now.status is not None and not same_ssh_path_status(now.status, expected.status):
        raise EmbeddedTrustFileError()


def _save_supported(path: Path, snapshot: _Snapshot) -> bool:
    if os.name == "nt":
        return windows_trust_save_supported(path, missing=snapshot.status is None)
    if os.name != "posix":
        return False
    try:
        parent = path.parent.lstat()
        return (
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == os.getuid()
            and not stat.S_IMODE(parent.st_mode) & 0o022
            and (snapshot.status is None or snapshot.status.st_uid == os.getuid())
        )
    except OSError:
        return snapshot.status is None and can_create_parent(path)


def can_create_parent(path: Path) -> bool:
    return can_create_windows_parent(path) if os.name == "nt" else can_create_posix_parent(path)


def _replacement(data: bytes, host: str, addr: str, port: int, key) -> bytes | None:
    return replace_host_records(data, host, addr, port, key)


def _commit(path: Path, snapshot: _Snapshot, data: bytes, *, create_parent: bool = False) -> None:
    """Private staging and guarded replace; not an interprocess compare-and-swap."""
    with _WRITE_LOCK:
        if not _save_supported(path, snapshot):
            raise EmbeddedTrustFileError()
        if os.name == "nt":
            try:
                commit_windows_trust(
                    path, snapshot.data, snapshot.status, data, create_parent=create_parent
                )
            except (OSError, ValueError, WindowsSshFileError):
                raise EmbeddedTrustFileError() from None
            return
        if create_parent:
            create_parent_after_consent(path)
        parent_status = path.parent.lstat()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stage = ".termana-host-key-" + uuid4().hex
        created = False
        try:
            opened = os.fstat(directory)
            if (opened.st_dev, opened.st_ino) != (parent_status.st_dev, parent_status.st_ino):
                raise EmbeddedTrustFileError()
            _unchanged(path, snapshot)
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            created = True
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _unchanged(path, snapshot)
            current_parent = path.parent.lstat()
            if (current_parent.st_dev, current_parent.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ) or not _save_supported(path, snapshot):
                raise EmbeddedTrustFileError()
            if snapshot.status is None:
                # Do not replace an independently created file, even in the
                # final check-to-commit interval. The hard link is no-clobber.
                os.link(stage, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.replace(stage, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                created = False
        finally:
            try:
                if created:
                    os.unlink(stage, dir_fd=directory)
            finally:
                os.close(directory)


class EmbeddedHostTrust:
    """One endpoint's one-shot question; no automatic trust or rekey approval."""

    def __init__(
        self,
        path: Path,
        endpoint: SshEndpoint,
        provider: EmbeddedHostKeyProvider,
    ) -> None:
        self.path = path
        self.endpoint = endpoint
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or type(endpoint) is not SshEndpoint
            or not callable(provider)
        ):
            raise ValueError("Host trust requires an absolute path, endpoint and provider.")
        self._provider = provider
        self._snapshot = _snapshot(path)
        self.known_hosts = asyncssh.import_known_hosts(self._snapshot.data.decode("utf-8"))
        # A lookup-only anchor prevents public match() from losing port-specific
        # revocation-only entries on fallback. Never returned as trusted data.
        anchor = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        token = endpoint.host if endpoint.port == 22 else f"[{endpoint.host}]:{endpoint.port}"
        self._policy_known_hosts = asyncssh.import_known_hosts(
            self._snapshot.data.decode("utf-8")
            + "\n"
            + token
            + " "
            + anchor.export_public_key().decode("ascii")
        )
        self._asked = False
        self._key = None
        self._data: bytes | None = None
        self._prepared = False
        self._creates_directory = False

    def match_known_hosts(self, host: str, addr: str, port: int | None):
        """Use public matching while retaining all port-specific trust policy."""
        if host != self.endpoint.host or port != (
            None if self.endpoint.port == 22 else self.endpoint.port
        ):
            raise EmbeddedTrustFileError()
        matches = self.known_hosts.match(host, addr, port)
        anchored = self._policy_known_hosts.match(host, addr, port)
        # Only original regular keys are trusted. Merge policy metadata, never
        # the anchor, into CA/revocation/certificate fields.
        return (
            matches[0],
            *[
                matches[index] + [item for item in anchored[index] if item not in matches[index]]
                for index in range(1, 7)
            ],
        )

    def validate(self, host: str, addr: str, port: int, key) -> bool:
        if host != self.endpoint.host or port != self.endpoint.port:
            return False
        if self._asked:
            return self._key is not None and key == self._key
        if self._prepared:
            return False  # Don't introduce a new trust question after credentials.
        self._asked = True
        matches = self.match_known_hosts(host, addr, None if port == 22 else port)
        if key in matches[2] or matches[1] or any(matches[3:]):
            return False  # Never override a CA/certificate/revocation policy.
        data = _replacement(self._snapshot.data, host, addr, port, key)
        save = data is not None and _save_supported(self.path, self._snapshot)
        question = EmbeddedHostKeyQuestion(
            self.endpoint,
            key.get_algorithm(),
            key.get_fingerprint("sha256"),
            bool(matches[0]),
            tuple(item.get_fingerprint("sha256") for item in matches[0][:8]),
            save,
            save and can_create_parent(self.path),
        )
        # The callback contract is synchronous: coroutine/non-bool truthiness
        # must never authorize a key. The library still verifies its signature.
        answer = self._provider(question)
        if inspect.iscoroutine(answer):
            answer.close()
        if answer is not True:
            return False
        _unchanged(self.path, self._snapshot)
        self._key = key
        self._data = data if save else None
        self._creates_directory = question.creates_directory
        return True

    async def prepare_credentials(self, connection) -> None:
        if self._prepared:
            return
        try:
            if self._key is not None:
                if connection.get_server_host_key() != self._key:
                    raise EmbeddedTrustFileError()
                if self._data is not None:
                    await asyncio.to_thread(
                        _commit,
                        self.path,
                        self._snapshot,
                        self._data,
                        create_parent=self._creates_directory,
                    )
                else:
                    await asyncio.to_thread(_unchanged, self.path, self._snapshot)
            else:
                await asyncio.to_thread(_unchanged, self.path, self._snapshot)
        except (OSError, ValueError, UnicodeError):
            raise EmbeddedTrustFileError() from None
        self._prepared = True
