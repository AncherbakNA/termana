"""Detect and safely remove obsolete OpenSSH host-key entries."""

import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from termana.credentials.ssh_file_metadata import same_ssh_handle_status, same_ssh_path_status
from termana.models.connection import DEFAULT_SSH_PORT
from termana.terminal.command import ShellCommand
from termana.terminal.ssh_command import resolve_system_ssh_keygen_program

MAX_TOOL_OUTPUT_BYTES: Final = 32 * 1024
MAX_HOST_KEY_DIAGNOSTIC_BYTES: Final = 64 * 1024
MAX_KNOWN_HOSTS_FILE_BYTES: Final = 8 * 1024 * 1024
MAX_RECOVERY_LINE_NUMBER: Final = 100_000
TOOL_TIMEOUT_MS: Final = 5000

_CHANGED_WARNING: Final = "REMOTE HOST IDENTIFICATION HAS CHANGED!"
_CHANGED_HEADER: Final = re.compile(
    r"\A@{20,100}\n@[ \t]+WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED![ \t]+@\n@{20,100}\n"
)
_HOST_CHANGED: Final = re.compile(
    r"^Host key for (?P<subject>.+?) has changed and "
    r"you have requested strict checking\.\s*$",
    re.MULTILINE,
)
_VERIFICATION_FAILED: Final = "Host key verification failed."
_FINGERPRINT: Final = re.compile(
    r"The fingerprint for the (?P<key_type>[A-Za-z0-9@._+-]+) key sent by "
    r"the remote host is[ \t]*\n[ \t]*"
    r"(?P<fingerprint>SHA256:[A-Za-z0-9+/=]+|"
    r"MD5:(?:[0-9A-Fa-f]{2}:){15}[0-9A-Fa-f]{2})\.?",
    re.MULTILINE,
)
_OFFENDING_KEY: Final = re.compile(
    r"^Offending\s+(?P<key_type>\S+)\s+key in "
    r"(?P<path>.+):(?P<line_number>[1-9][0-9]{0,9})[ \t]*$",
    re.MULTILINE,
)
_LOG_ESCAPE: Final = re.compile(rb"\\\\|\\[0-3][0-7]{2}")


def _decode_log_escape(match: re.Match[bytes]) -> bytes:
    """Undo OpenSSH VIS_OCTAL/escaped slash without introducing control records."""
    escaped = match.group()
    if escaped == b"\\\\":
        return b"\\"
    value = int(escaped[1:], 8)
    if value < 128 and value != ord("\\"):
        raise ValueError("A host-key diagnostic contains unexpected ASCII escapes")
    return bytes((value,))


@dataclass(frozen=True, slots=True)
class SshHostKeyMismatch:
    """Structured details extracted from an OpenSSH security warning."""

    received_key_type: str | None
    received_fingerprint: str | None
    stored_key_type: str
    known_hosts_path: Path
    line_number: int
    host_key_subject: str


def has_ssh_host_key_warning(output: bytes) -> bool:
    """Recognize advisory-only text; never use this to authorize a trust edit."""
    bounded = output[-MAX_HOST_KEY_DIAGNOSTIC_BYTES:]
    if len(output) >= MAX_HOST_KEY_DIAGNOSTIC_BYTES:
        _, _, bounded = bounded.partition(b"\n")
    for line in bounded.split(b"\n")[:-1]:
        line = line.rstrip(b"\r")
        if line == _VERIFICATION_FAILED.encode("ascii") or (
            line.startswith(b"@")
            and line.endswith(b"@")
            and line.strip(b"@ \t") == b"WARNING: " + _CHANGED_WARNING.encode("ascii")
        ):
            return True
    return False


def parse_ssh_host_key_mismatch(output: bytes) -> SshHostKeyMismatch | None:
    """Return mismatch details only for a complete standard OpenSSH warning."""
    if len(output) >= MAX_HOST_KEY_DIAGNOSTIC_BYTES:
        return None
    try:
        # OpenSSH can octal-escape individual UTF-8 bytes; decode them before
        # interpreting UTF-8. One pass preserves literal backslash/digit names.
        text = _LOG_ESCAPE.sub(_decode_log_escape, output).decode("utf-8", errors="strict")
    except ValueError:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(not char.isprintable() and char not in "\n\t" for char in text):
        return None
    # -E also records remote disconnect/debug text with a local prefix. A
    # warning embedded inside those records is not a local host-key failure.
    # Accept a standalone standard block, never a retained/truncated log tail
    # or a block surrounded by authentication/channel/disconnect messages.
    if not _CHANGED_HEADER.match(text) or not text.rstrip("\n").endswith(
        "\n" + _VERIFICATION_FAILED
    ):
        return None
    hosts = list(_HOST_CHANGED.finditer(text))
    fingerprints = list(_FINGERPRINT.finditer(text))
    offending = list(_OFFENDING_KEY.finditer(text))
    if (
        text.count(_CHANGED_WARNING) != 1
        or text.count(_VERIFICATION_FAILED) != 1
        or len(hosts) != 1
        or len(offending) != 1
        or len(fingerprints) > 1
    ):
        return None
    host_match, offending_match = hosts[0], offending[0]
    fingerprint_match = fingerprints[0] if fingerprints else None
    if offending_match.end() > host_match.start() or (
        fingerprint_match is not None and fingerprint_match.end() > offending_match.start()
    ):
        return None
    line_number = int(offending_match.group("line_number"))
    if line_number > MAX_RECOVERY_LINE_NUMBER:
        return None

    return SshHostKeyMismatch(
        received_key_type=(
            fingerprint_match.group("key_type") if fingerprint_match is not None else None
        ),
        received_fingerprint=(
            fingerprint_match.group("fingerprint") if fingerprint_match is not None else None
        ),
        stored_key_type=offending_match.group("key_type"),
        known_hosts_path=Path(offending_match.group("path").strip()),
        line_number=line_number,
        host_key_subject=host_match.group("subject").strip(),
    )


def known_hosts_target(host: str, port: int) -> str:
    """Build the host token used by OpenSSH's known_hosts database."""
    normalized_host = host.strip()
    if not normalized_host:
        raise ValueError("SSH host cannot be empty")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")

    if normalized_host.startswith("[") and normalized_host.endswith("]"):
        normalized_host = normalized_host[1:-1]
    if port == DEFAULT_SSH_PORT:
        return normalized_host
    return f"[{normalized_host}]:{port}"


def build_known_hosts_command(
    *,
    program: str,
    operation: str,
    target: str,
    known_hosts_path: Path,
) -> ShellCommand:
    """Build one ssh-keygen command without passing text through a shell."""
    if operation not in {"-F", "-R"}:
        raise ValueError("Unsupported ssh-keygen operation")
    if not Path(program).is_absolute():
        raise ValueError("ssh-keygen must use an absolute executable path")
    if not known_hosts_path.is_absolute():
        raise ValueError("The known_hosts path must be absolute")
    return ShellCommand(
        program=program,
        arguments=(operation, target, "-f", str(known_hosts_path)),
    )


def known_hosts_lookup_is_safe(
    output: bytes,
    *,
    target: str,
    truncated: bool = False,
) -> bool:
    """Accept only complete lookup output containing single exact or hashed hosts."""
    if truncated:
        return False
    text = output.decode("utf-8", errors="replace")
    entries = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(entries) != 1:
        return False

    for entry in entries:
        fields = entry.split()
        if len(fields) < 3:
            return False
        if fields[0] in {"@cert-authority", "@revoked"}:
            return False
        host_field = fields[0]
        if host_field.startswith("|1|"):
            hash_fields = host_field.split("|")
            if (
                len(hash_fields) != 4
                or not hash_fields[2]
                or not hash_fields[3]
                or "," in host_field
            ):
                return False
            continue
        if any(character in host_field for character in ",*?!"):
            return False
        if host_field.casefold() != target.casefold():
            return False
    return True


def _known_hosts_lookup_entries(output: bytes) -> list[str]:
    """Return only data records from ssh-keygen lookup output."""
    text = output.decode("utf-8", errors="replace")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _stored_key_type_matches(diagnostic_type: str, file_key_type: str) -> bool:
    """Match OpenSSH's friendly diagnostic label to a known_hosts key field."""
    normalized_diagnostic = diagnostic_type.strip().upper()
    normalized_file_type = file_key_type.strip().lower()
    prefixes = {
        "DSA": ("ssh-dss",),
        "ECDSA": ("ecdsa-sha2-",),
        "ED25519": ("ssh-ed25519",),
        "RSA": ("ssh-rsa", "rsa-sha2-"),
        "XMSS": ("ssh-xmss",),
    }
    expected_prefixes = prefixes.get(normalized_diagnostic)
    if expected_prefixes is None:
        return normalized_file_type == diagnostic_type.strip().lower()
    return normalized_file_type.startswith(expected_prefixes)


def _default_known_hosts_path() -> Path:
    """Return the only trust database that the first recovery flow may edit."""
    from termana.paths import known_hosts_file_path
    return known_hosts_file_path()


def _find_ssh_keygen() -> str | None:
    """Resolve only the trusted system helper, never a PATH-selected program."""
    return resolve_system_ssh_keygen_program()


def _normalized_path(path: Path) -> Path:
    """Normalize spelling without following a potentially unsafe symlink."""
    normalized = os.path.abspath(os.path.expanduser(str(path)))
    if sys.platform == "darwin":
        # macOS filesystems may return decomposed spelling for the same filename.
        # Do not apply this on Linux, where both spellings can be distinct files.
        normalized = unicodedata.normalize("NFC", normalized)
    return Path(normalized)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_size,
    )


def _validate_file_metadata(metadata: os.stat_result) -> None:
    """Apply the same trust policy to both the path and the opened descriptor."""
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("Automatic recovery does not edit a symlinked known_hosts file.")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("The known_hosts path is not a regular file.")
    if metadata.st_size > MAX_KNOWN_HOSTS_FILE_BYTES:
        raise ValueError("The known_hosts file is too large for automatic recovery.")
    if metadata.st_nlink != 1:
        raise ValueError("Automatic recovery does not edit a hard-linked known_hosts file.")
    if hasattr(os, "getuid"):
        if metadata.st_uid != os.getuid():
            raise ValueError("The known_hosts file is not owned by the current user.")
        if metadata.st_mode & 0o022:
            raise ValueError("The known_hosts file is writable by other users.")


class _RemovalPhase(StrEnum):
    FIND = "find"
    REMOVE = "remove"
    VERIFY = "verify"


@dataclass(slots=True)
class _RemovalRequest:
    """State retained across the asynchronous verification subprocesses."""

    context: object
    target: str
    line_number: int
    stored_key_type: str
    reported_entry: str
    file_identity: tuple[int, int, int, int, int]
    parent_identity: tuple[int, int]
    backup_identity: tuple[int, int, int, int, int] | None
    updated_file_identity: tuple[int, int, int, int, int] | None = None
    updated_backup_identity: tuple[int, int, int, int, int] | None = None
    phase: _RemovalPhase = _RemovalPhase.FIND
    output: bytearray = field(default_factory=bytearray)
    process_error: str = ""
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False


class KnownHostsUpdater(QObject):
    """Verify, remove, and recheck one default known_hosts entry asynchronously."""

    removal_finished = Signal(object, bool, str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        known_hosts_path: Path | None = None,
        ssh_keygen_program: str | None = None,
    ) -> None:
        """Create an updater with an injectable file and executable for tests."""
        super().__init__(parent)
        selected_path = known_hosts_path or _default_known_hosts_path()
        self.known_hosts_path = _normalized_path(selected_path)
        self._ssh_keygen_program = ssh_keygen_program or _find_ssh_keygen()
        self._process: QProcess | None = None
        self._request: _RemovalRequest | None = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._handle_timeout)

    @property
    def is_running(self) -> bool:
        """Return whether a recovery request currently owns a helper process."""
        return self._request is not None

    def manages_path(self, diagnostic_path: Path) -> bool:
        """Accept only the exact default policy path, never an output-selected path."""
        return _normalized_path(diagnostic_path) == self.known_hosts_path

    def remove_host_key(
        self,
        context: object,
        *,
        host: str,
        port: int,
        line_number: int,
        stored_key_type: str,
    ) -> None:
        """Start a guarded find-remove-verify operation for one SSH destination."""
        if self._request is not None:
            self._report_result(context, False, "Another known_hosts update is already running.")
            return
        if self._ssh_keygen_program is None:
            self._report_result(context, False, "The ssh-keygen program was not found.")
            return
        if not Path(self._ssh_keygen_program).is_absolute():
            self._report_result(
                context,
                False,
                "The ssh-keygen program path is not absolute.",
            )
            return

        try:
            target = known_hosts_target(host, port)
            parent_identity = self._safe_parent_identity()
            identity = self._safe_file_identity()
            backup_identity = self._validate_backup_path()
            reported_entry = self._validate_reported_entry(
                target=target,
                line_number=line_number,
                stored_key_type=stored_key_type,
                expected_identity=identity,
            )
        except (OSError, ValueError) as error:
            self._report_result(context, False, str(error))
            return

        self._request = _RemovalRequest(
            context=context,
            target=target,
            line_number=line_number,
            stored_key_type=stored_key_type,
            reported_entry=reported_entry,
            file_identity=identity,
            parent_identity=parent_identity,
            backup_identity=backup_identity,
        )
        self._start_phase()

    def cancel(self) -> None:
        """Stop a running helper; completion remains asynchronous."""
        if self._request is None:
            return
        self._request.process_error = "The known_hosts update was cancelled."
        self._request.cancelled = True
        if self._process is None or self._process.state() == QProcess.ProcessState.NotRunning:
            self._complete(False)
            return
        self._process.kill()

    def _safe_file_identity(self) -> tuple[int, int, int, int, int]:
        """Require an existing, regular, user-owned file and reject symlinks."""
        file_stat = self.known_hosts_path.lstat()
        _validate_file_metadata(file_stat)
        return _file_identity(file_stat)

    def _safe_parent_identity(self) -> tuple[int, int]:
        """Refuse a redirected or other-user-writable immediate trust directory."""
        metadata = self.known_hosts_path.parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Automatic recovery requires a non-symlinked known_hosts directory.")
        if hasattr(os, "getuid") and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o022):
            raise ValueError(
                "The known_hosts directory must be owned and writable only by this user."
            )
        return metadata.st_dev, metadata.st_ino

    def _read_verified_file(self, expected_identity: tuple[int, int, int, int, int]) -> bytes:
        """Bound reads and detect replacement/growth between validation and use.

        This pins the read, not ssh-keygen's later pathname-based mutation. It is
        deliberately not advertised as an atomic transaction against other writers.
        """
        selected = self.known_hosts_path.lstat()
        _validate_file_metadata(selected)
        if _file_identity(selected) != expected_identity:
            raise ValueError(
                "The known_hosts file changed during verification; no key was removed."
            )
        flags = os.O_RDONLY
        for flag in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "O_BINARY"):
            flags |= getattr(os, flag, 0)
        descriptor = os.open(self.known_hosts_path, flags)
        try:
            before = os.fstat(descriptor)
            _validate_file_metadata(before)
            # On Windows 3.14, path ctime is creation time while handle ctime
            # can be metadata-change time. Compare birthtime across APIs;
            # POSIX ctime and same-handle before/after ctime remain checked.
            if not same_ssh_path_status(before, selected):
                raise ValueError(
                    "The known_hosts file changed during verification; no key was removed."
                )
            data = bytearray()
            while len(data) <= MAX_KNOWN_HOSTS_FILE_BYTES:
                chunk = os.read(
                    descriptor, min(64 * 1024, MAX_KNOWN_HOSTS_FILE_BYTES + 1 - len(data))
                )
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_KNOWN_HOSTS_FILE_BYTES:
                raise ValueError("The known_hosts file is too large for automatic recovery.")
            after = os.fstat(descriptor)
            current = self.known_hosts_path.lstat()
            for metadata in (after, current):
                _validate_file_metadata(metadata)
            if (
                not same_ssh_handle_status(before, after)
                or _file_identity(current) != expected_identity
                or not same_ssh_path_status(after, current)
            ):
                raise ValueError(
                    "The known_hosts file changed during verification; no key was removed."
                )
            return bytes(data)
        finally:
            os.close(descriptor)

    def _validate_backup_path(self) -> tuple[int, int, int, int, int] | None:
        """Refuse a backup target that ssh-keygen could overwrite unsafely."""
        backup_path = Path(f"{self.known_hosts_path}.old")
        try:
            backup_stat = backup_path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(backup_stat.st_mode):
            raise ValueError("Automatic recovery does not overwrite a symlinked .old backup.")
        if not stat.S_ISREG(backup_stat.st_mode):
            raise ValueError("The existing .old backup is not a regular file.")
        if backup_stat.st_nlink != 1:
            raise ValueError("Automatic recovery does not overwrite a hard-linked .old backup.")
        if hasattr(os, "getuid") and backup_stat.st_uid != os.getuid():
            raise ValueError("The existing .old backup is not owned by the current user.")
        if hasattr(os, "getuid") and backup_stat.st_mode & 0o022:
            raise ValueError("The existing .old backup is writable by other users.")
        return _file_identity(backup_stat)

    def _validate_reported_entry(
        self,
        *,
        target: str,
        line_number: int,
        stored_key_type: str,
        expected_identity: tuple[int, int, int, int, int],
    ) -> str:
        """Bind the terminal diagnostic to one safe record in the local file."""
        if isinstance(line_number, bool) or not isinstance(line_number, int) or line_number < 1:
            raise ValueError("The reported known_hosts line number is invalid.")
        if line_number > MAX_RECOVERY_LINE_NUMBER:
            raise ValueError(
                "The reported known_hosts line is beyond the automatic recovery limit."
            )
        if not stored_key_type.strip():
            raise ValueError("The reported saved key type is invalid.")

        text = self._read_verified_file(expected_identity).decode("utf-8", errors="strict")
        # Only retain the one reported record, not a list with potentially
        # millions of empty/comment lines from an otherwise bounded file.
        start = 0
        for _ in range(line_number - 1):
            newline = text.find("\n", start)
            if newline < 0:
                raise ValueError("The reported known_hosts line no longer exists.")
            start = newline + 1
        if start >= len(text):
            raise ValueError("The reported known_hosts line no longer exists.")
        end = text.find("\n", start)
        entry = text[start : end if end >= 0 else len(text)].strip()
        fields = entry.split()
        if len(fields) < 3 or fields[0].startswith("@"):
            raise ValueError("The reported known_hosts line cannot be edited automatically.")

        host_field, file_key_type = fields[:2]
        if host_field.startswith("|1|"):
            hash_fields = host_field.split("|")
            if (
                len(hash_fields) != 4
                or not hash_fields[2]
                or not hash_fields[3]
                or "," in host_field
            ):
                raise ValueError("The reported hashed known_hosts entry is malformed.")
        elif (
            any(character in host_field for character in ",*?!")
            or host_field.casefold() != target.casefold()
        ):
            raise ValueError(
                "The reported known_hosts line does not belong only to this SSH endpoint."
            )
        if not _stored_key_type_matches(stored_key_type, file_key_type):
            raise ValueError(
                "The reported saved key type does not match the local known_hosts entry."
            )
        return entry

    def _start_phase(self) -> None:
        """Launch the command for the active state-machine phase."""
        request = self._request
        program = self._ssh_keygen_program
        if request is None or program is None:
            return

        operation = "-R" if request.phase is _RemovalPhase.REMOVE else "-F"
        command = build_known_hosts_command(
            program=program,
            operation=operation,
            target=request.target,
            known_hosts_path=self.known_hosts_path,
        )
        request.output.clear()
        request.process_error = ""
        request.output_truncated = False

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process = process
        process.readyReadStandardOutput.connect(self._handle_ready_output)
        process.errorOccurred.connect(self._handle_process_error)
        process.finished.connect(self._handle_process_finished)
        self._timeout.start(TOOL_TIMEOUT_MS)
        process.start(command.program, list(command.arguments))

    def _handle_ready_output(self) -> None:
        """Drain output only from the helper that owns the active phase."""
        process = self.sender()
        if isinstance(process, QProcess):
            self._read_process_output(process)

    def _read_process_output(self, process: QProcess) -> None:
        """Retain a bounded diagnostic tail from the current helper process."""
        request = self._request
        if process is not self._process or request is None:
            return
        request.output.extend(bytes(process.readAllStandardOutput()))
        overflow = len(request.output) - MAX_TOOL_OUTPUT_BYTES
        if overflow > 0:
            request.output_truncated = True
            del request.output[:overflow]

    def _handle_process_error(
        self,
        error: QProcess.ProcessError,
    ) -> None:
        """Remember process errors and finish when the helper cannot start."""
        process = self.sender()
        request = self._request
        if not isinstance(process, QProcess) or process is not self._process or request is None:
            return
        if not request.timed_out and not request.cancelled:
            request.process_error = process.errorString()
        if error == QProcess.ProcessError.FailedToStart:
            self._complete(False)

    def _handle_process_finished(
        self,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        """Advance only after interpreting the current helper result."""
        process = self.sender()
        request = self._request
        if not isinstance(process, QProcess) or request is None or process is not self._process:
            return
        self._timeout.stop()
        self._read_process_output(process)
        process.deleteLater()
        self._process = None
        self._advance_after_process(request, exit_code, exit_status)

    def _advance_after_process(
        self,
        request: _RemovalRequest,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        """Advance one verified phase result, unless cancellation already won."""
        if request is not self._request:
            return
        if request.cancelled or request.timed_out:
            self._complete(False)
            return
        if exit_status != QProcess.ExitStatus.NormalExit:
            self._complete(False)
            return

        if request.phase is _RemovalPhase.FIND:
            if exit_code == 1:
                self._complete(
                    False,
                    "The host-key warning reported a saved entry, but the local "
                    "ssh-keygen lookup did not find it. No file was changed; "
                    "reconnect manually after inspecting your SSH configuration.",
                )
                return
            if exit_code != 0:
                self._complete(False)
                return
            if not known_hosts_lookup_is_safe(
                bytes(request.output),
                target=request.target,
                truncated=request.output_truncated,
            ):
                self._complete(
                    False,
                    "The known_hosts matches could not be classified safely. "
                    "They may be shared, wildcarded, marked, or too numerous; "
                    "edit the file manually.",
                )
                return
            if request.reported_entry not in _known_hosts_lookup_entries(bytes(request.output)):
                self._complete(
                    False,
                    "The reported known_hosts line was not returned by the local "
                    "ssh-keygen lookup; no key was removed.",
                )
                return
            try:
                if self._safe_parent_identity() != request.parent_identity:
                    raise ValueError(
                        "The known_hosts directory changed during verification; no key was removed."
                    )
                if self._safe_file_identity() != request.file_identity:
                    raise ValueError(
                        "The known_hosts file changed during verification; no key was removed."
                    )
                if self._validate_backup_path() != request.backup_identity:
                    raise ValueError(
                        "The .old backup changed during verification; no key was removed."
                    )
                if (
                    self._validate_reported_entry(
                        target=request.target,
                        line_number=request.line_number,
                        stored_key_type=request.stored_key_type,
                        expected_identity=request.file_identity,
                    )
                    != request.reported_entry
                ):
                    raise ValueError(
                        "The reported known_hosts entry changed during verification; "
                        "no key was removed."
                    )
            except (OSError, ValueError) as error:
                self._complete(False, str(error))
                return
            request.phase = _RemovalPhase.REMOVE
            self._start_phase()
            return

        if request.phase is _RemovalPhase.REMOVE:
            if exit_code != 0:
                self._complete(False)
                return
            try:
                if self._safe_parent_identity() != request.parent_identity:
                    raise ValueError("The known_hosts directory changed during the update.")
                request.updated_file_identity = self._safe_file_identity()
                request.updated_backup_identity = self._validate_backup_path()
                if request.updated_backup_identity is None:
                    raise ValueError(
                        "The known_hosts update did not leave its expected .old backup."
                    )
            except (OSError, ValueError) as error:
                self._complete(False, str(error))
                return
            request.phase = _RemovalPhase.VERIFY
            self._start_phase()
            return

        if exit_code == 1:
            try:
                if (
                    self._safe_parent_identity() != request.parent_identity
                    or self._safe_file_identity() != request.updated_file_identity
                    or self._validate_backup_path() != request.updated_backup_identity
                ):
                    raise ValueError(
                        "The known_hosts file or backup changed during final verification."
                    )
            except (OSError, ValueError) as error:
                self._complete(False, str(error))
                return
            backup_path = Path(f"{self.known_hosts_path}.old")
            message = "No matching saved SSH host key remains."
            if backup_path.is_file():
                message += f" Latest-operation backup: {backup_path}"
            self._complete(True, message)
            return
        self._complete(
            False,
            "The saved host key still appears in known_hosts after ssh-keygen completed.",
        )

    def _handle_timeout(self) -> None:
        """Kill a stuck helper and complete after QProcess reports it stopped."""
        request = self._request
        process = self._process
        if request is None or process is None:
            return
        request.timed_out = True
        request.process_error = "ssh-keygen timed out."
        process.kill()

    def _complete(self, succeeded: bool, message: str | None = None) -> None:
        """Publish one result and clear the active request exactly once."""
        request = self._request
        if request is None:
            return
        self._timeout.stop()
        if self._process is not None and self._process.state() == QProcess.ProcessState.NotRunning:
            self._process.deleteLater()
            self._process = None
        output = request.output.decode("utf-8", errors="replace").strip()
        if message is not None:
            result_message = message
        elif not succeeded and request.process_error:
            result_message = request.process_error
        else:
            result_message = output or request.process_error
        if not succeeded and not result_message:
            result_message = "ssh-keygen could not update the known_hosts file."
        if not succeeded and request.phase is not _RemovalPhase.FIND:
            backup_path = Path(f"{self.known_hosts_path}.old")
            if request.phase is _RemovalPhase.VERIFY:
                mutation_warning = (
                    "The known_hosts file was modified, but the result could not be verified."
                )
            else:
                mutation_warning = "ssh-keygen may already have modified the known_hosts file."
            if backup_path.is_file():
                mutation_warning += f" Inspect the latest-operation backup: {backup_path}."
            result_message = f"{mutation_warning} {result_message}"
        context = request.context
        self._request = None
        self.removal_finished.emit(context, succeeded, result_message)

    def _report_result(self, context: object, succeeded: bool, message: str) -> None:
        """Publish a validation result without allocating another process or timer."""
        self.removal_finished.emit(context, succeeded, message)
