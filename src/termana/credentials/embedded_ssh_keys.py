"""Explicit, lazy key/agent authentication for the experimental SSH transport.

No default keys, SSH config, sidecar certificates or environment agent are read.
The transport calls this source only after verifying the server host key.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections import deque
from pathlib import Path
from typing import Final

import asyncssh
from asyncssh.agent import SSH_AGENT_IDENTITIES_ANSWER

from termana.credentials.embedded_ssh_auth import (
    MAX_EMBEDDED_PASSWORD_BYTES,
    EmbeddedPasswordProvider,
    require_embedded_auth_contract,
)
from termana.credentials.embedded_ssh_key_import import (
    MAX_EMBEDDED_KEY_BCRYPT_ROUNDS as MAX_EMBEDDED_KEY_BCRYPT_ROUNDS,
)
from termana.credentials.embedded_ssh_key_import import (
    MAX_EMBEDDED_KEY_FILE_BYTES as MAX_EMBEDDED_KEY_FILE_BYTES,
)
from termana.credentials.embedded_ssh_key_import import (
    MAX_EMBEDDED_RSA_MODULUS_BITS as MAX_EMBEDDED_RSA_MODULUS_BITS,
)
from termana.credentials.embedded_ssh_key_import import (
    MAX_EMBEDDED_RSA_MODULUS_BYTES as MAX_EMBEDDED_RSA_MODULUS_BYTES,
)
from termana.credentials.embedded_ssh_key_import import (
    MISSING_EMBEDDED_KEY_PASSPHRASE,
    import_bounded_private_key,
)
from termana.credentials.embedded_ssh_key_import import (
    EmbeddedIdentityError as EmbeddedIdentityError,
)
from termana.credentials.embedded_ssh_key_import import (
    validate_embedded_key_envelope as validate_embedded_key_envelope,
)
from termana.credentials.secret_store import MAX_SSH_PASSWORD_CHARACTERS
from termana.credentials.ssh_file_metadata import (
    SSH_FILE_HANDLE_FIELDS,
    SSH_FILE_PATH_FIELDS,
    same_ssh_path_status,
)
from termana.credentials.windows_ssh_files import (
    WindowsSshFileError,
    open_windows_ssh_file,
    windows_ssh_security_stamp,
)

MAX_EMBEDDED_PASSPHRASE_ATTEMPTS: Final = 3
MAX_EMBEDDED_AGENT_IDENTITIES: Final = 16
MAX_EMBEDDED_AGENT_PACKET_BYTES: Final = 512 * 1024
EMBEDDED_AGENT_OPERATION_TIMEOUT_SECONDS: Final = 5
EMBEDDED_AGENT_CLOSE_TIMEOUT_SECONDS: Final = 2
_KEY_HANDLE_STATUS_FIELDS: Final = SSH_FILE_HANDLE_FIELDS
_KEY_PATH_STATUS_FIELDS: Final = SSH_FILE_PATH_FIELDS


def read_embedded_private_key(path: Path) -> bytes:
    """Read one regular, bounded file without changing it or following a symlink.

    POSIX keys must belong to this user and have no group/other permissions.
    Windows checks the exact handle's owner/DACL without repairing permissions.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise EmbeddedIdentityError()
    try:
        # O_NONBLOCK prevents a FIFO replacement from blocking before fstat.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        link_status = None if os.name == "nt" else path.lstat()
        if link_status is not None and not stat.S_ISREG(link_status.st_mode):
            raise EmbeddedIdentityError()
        descriptor = open_windows_ssh_file(path) if os.name == "nt" else os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if link_status is not None and not _same_key_path_status(before, link_status):
                raise EmbeddedIdentityError()
            if (
                not stat.S_ISREG(before.st_mode)
                or not 0 < before.st_size <= MAX_EMBEDDED_KEY_FILE_BYTES
            ):
                raise EmbeddedIdentityError()
            if os.name == "posix" and (
                before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077
            ):
                raise EmbeddedIdentityError()
            security = (
                windows_ssh_security_stamp(descriptor, private=True) if os.name == "nt" else None
            )
            chunks, size = [], 0
            while chunk := os.read(descriptor, min(65536, MAX_EMBEDDED_KEY_FILE_BYTES + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_EMBEDDED_KEY_FILE_BYTES:
                    raise EmbeddedIdentityError()
            after = os.fstat(descriptor)
            if os.name == "nt" and security != windows_ssh_security_stamp(descriptor, private=True):
                raise EmbeddedIdentityError()
            if size != before.st_size or any(
                getattr(before, f) != getattr(after, f) for f in _KEY_HANDLE_STATUS_FIELDS
            ):
                raise EmbeddedIdentityError()
            if os.name != "nt" and not _same_key_path_status(after, path.lstat()):
                raise EmbeddedIdentityError()
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except (OSError, ValueError, WindowsSshFileError):
        raise EmbeddedIdentityError() from None


def _same_key_path_status(handle_status: os.stat_result, path_status: os.stat_result) -> bool:
    """Compare like-for-like metadata across handle/path APIs.

    On Python 3.14 Windows, fstat's ctime can be metadata-change time while
    lstat's deprecated ctime is creation time. Keep ctime in the same-handle
    before/after guard; use birthtime for cross-API creation-time comparison.
    POSIX continues to compare ctime everywhere.
    """
    return same_ssh_path_status(handle_status, path_status, _KEY_PATH_STATUS_FIELDS)


class _BoundedAgentReader:
    """Reject oversized announced agent frames before allocating their payload."""

    def __init__(self, reader) -> None:
        self._reader = reader

    async def readexactly(self, size: int) -> bytes:
        if type(size) is not int or not 0 <= size <= MAX_EMBEDDED_AGENT_PACKET_BYTES:
            raise EmbeddedIdentityError()
        data = await self._reader.readexactly(size)
        if (
            size >= 5
            and data[0] == SSH_AGENT_IDENTITIES_ANSWER
            and int.from_bytes(data[1:5], "big") > MAX_EMBEDDED_AGENT_IDENTITIES
        ):
            # Preflight the count before AsyncSSH constructs identity objects.
            # Keep the reviewed library's decoder for the actual response.
            raise EmbeddedIdentityError()
        return data


class _TimedAgent(asyncssh.SSHAgentClient):
    """Bound public agent operations on cancellable Unix sockets, not Win32 pipes."""

    async def connect(self) -> None:
        await super().connect()
        # Per-instance reader wrapper, reviewed against pinned AsyncSSH 2.24.1.
        # _make_request reads the four-byte size, then readexactly(payload size).
        # Never patch global packet handlers or replace the library's decoder.
        self._reader = _BoundedAgentReader(self._reader)

    async def get_keys(self, identities=None):
        async with asyncio.timeout(EMBEDDED_AGENT_OPERATION_TIMEOUT_SECONDS):
            keys = await super().get_keys(identities)
        if len(keys) > MAX_EMBEDDED_AGENT_IDENTITIES:
            raise EmbeddedIdentityError()
        return keys

    async def sign(self, key_blob, data, flags=0):
        async with asyncio.timeout(EMBEDDED_AGENT_OPERATION_TIMEOUT_SECONDS):
            return await super().sign(key_blob, data, flags)


class EmbeddedKeySource:
    """One file attempt, then an explicitly opted-in agent; no persistence."""

    def __init__(
        self,
        identity_file: Path | None,
        agent_path: Path | None,
        passphrase_provider: EmbeddedPasswordProvider | None,
    ) -> None:
        require_embedded_auth_contract()
        self._identity_file = identity_file
        self._agent_path = agent_path
        self._provider = passphrase_provider
        self._file_attempted = False
        self._agent_attempted = False
        self._agent: _TimedAgent | None = None
        self._agent_keys: deque[asyncssh.SSHKeyPair] = deque()
        self._closed = False

    async def public_key_auth_requested(self) -> asyncssh.SSHKey | asyncssh.SSHKeyPair | None:
        if self._closed:
            return None
        if not self._file_attempted:
            self._file_attempted = True
            if self._identity_file is not None:
                key = await self._load_file()
                if self._closed:
                    return None
                if key is not None:
                    return key
        if not self._agent_attempted:
            self._agent_attempted = True
            if self._agent_path is not None:
                self._agent = _TimedAgent(str(self._agent_path))
                try:
                    keys = await self._agent.get_keys()
                except (OSError, ValueError, TimeoutError):
                    raise EmbeddedIdentityError() from None
                if self._closed:
                    return None
                self._agent_keys.extend(keys)
        return self._agent_keys.popleft() if self._agent_keys else None

    async def _load_file(self) -> asyncssh.SSHKey | None:
        data, passphrase = None, None
        try:
            data = await asyncio.to_thread(read_embedded_private_key, self._identity_file)
            if self._closed:
                return None
            validate_embedded_key_envelope(data)
            try:
                return await asyncio.to_thread(import_bounded_private_key, data)
            except asyncssh.KeyImportError as error:
                # This exact distinction is reviewed with pinned AsyncSSH 2.24.1.
                if str(error) != MISSING_EMBEDDED_KEY_PASSPHRASE:
                    raise EmbeddedIdentityError() from None
            for _ in range(MAX_EMBEDDED_PASSPHRASE_ATTEMPTS):
                provider = self._provider
                if self._closed or provider is None:
                    return None
                passphrase = await provider()
                if self._closed or passphrase is None:
                    return None
                if (
                    type(passphrase) is not str
                    or "\0" in passphrase
                    or len(passphrase) > MAX_SSH_PASSWORD_CHARACTERS
                    or len(passphrase.encode("utf-8")) > MAX_EMBEDDED_PASSWORD_BYTES
                ):
                    raise EmbeddedIdentityError()
                try:
                    return await asyncio.to_thread(import_bounded_private_key, data, passphrase)
                except (asyncssh.KeyEncryptionError, asyncssh.KeyImportError):
                    passphrase = None
            raise EmbeddedIdentityError()
        except Exception:
            raise EmbeddedIdentityError() from None
        finally:
            data = passphrase = None
            self._provider = None

    def close(self) -> None:
        self._closed = True
        self._provider = None
        self._agent_keys.clear()
        if self._agent is not None:
            self._agent.close()

    async def wait_closed(self) -> None:
        if self._agent is not None:
            try:
                async with asyncio.timeout(EMBEDDED_AGENT_CLOSE_TIMEOUT_SECONDS):
                    await self._agent.wait_closed()
            except (OSError, TimeoutError):
                pass
            finally:
                self._agent = None
