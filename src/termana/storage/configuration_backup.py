"""Bounded explicit configuration backups; never include passwords, keys or trust."""

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from termana.build_info import packaged_build
from termana.storage.atomic_file import replace_file_atomically
from termana.storage.command_snippet_store import CommandSnippetStore
from termana.storage.connection_store import ConnectionStore
from termana.storage.json_objects import reject_duplicate_json_keys
from termana.storage.settings_store import SettingsStore

FILES = {
    "settings.json": SettingsStore,
    "connections.json": ConnectionStore,
    "saved-commands.json": CommandSnippetStore,
}
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 9 * 1024 * 1024
PENDING = "configuration-restore.pending.zip"
JOURNAL = "configuration-restore.journal.json"
RECOVERY_JOURNAL = "configuration-recovery.journal.json"
BACKUPS = "configuration-backups"


class ConfigurationBackupError(RuntimeError):
    """A safe public error without serializing configuration contents."""


@dataclass(frozen=True)
class RestoreResult:
    files: tuple[str, ...]
    previous_configuration: Path


@dataclass(frozen=True)
class PreparedRestore:
    files: tuple[str, ...]
    archive: bytes = field(repr=False)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _reparse(info):
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _directory(path, *, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or _reparse(info):
        raise ConfigurationBackupError("Configuration backup directory is not a regular directory.")
    return path


def _read(path, limit=MAX_FILE_BYTES, *, missing=False):
    try:
        initial = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or _reparse(initial):
        raise ConfigurationBackupError(
            "Linked or non-regular configuration files are not supported."
        )
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if (initial.st_dev, initial.st_ino) != (actual.st_dev, actual.st_ino):
            raise ConfigurationBackupError("Configuration changed while it was being read.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ConfigurationBackupError("Configuration backup exceeds the supported size limit.")
    return data


def _write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _json(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)


def _validate(documents):
    if not documents or set(documents) - FILES.keys():
        raise ConfigurationBackupError("Backup contains no supported configuration files.")
    if sum(map(len, documents.values())) > MAX_TOTAL_BYTES:
        raise ConfigurationBackupError("Configuration backup exceeds the supported size limit.")
    # Validate an immutable snapshot, not live files that another operation could
    # replace. Existing codecs enforce strict schemas and future-version guards.
    with TemporaryDirectory(prefix="termana-config-validate-") as temporary:
        for name, data in documents.items():
            path = Path(temporary) / name
            _write_new(path, data)
            try:
                FILES[name](path).load()
            except Exception:
                raise ConfigurationBackupError(
                    "Backup configuration is invalid or from an unsupported version."
                ) from None


def _snapshot(directory):
    documents = {}
    for name in FILES:
        data = _read(directory / name, missing=True)
        if data is not None:
            documents[name] = data
    return documents


def _archive_documents(documents):
    manifest = dict(
        schema_version=1,
        build_id=packaged_build()["build_id"],
        files={
            name: dict(bytes=len(data), sha256=_digest(data)) for name, data in documents.items()
        },
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in documents.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
    return output.getvalue()


def create_backup(directory: Path, destination: Path) -> tuple[str, ...]:
    """Create a new private archive; credentials/index/known_hosts are excluded."""
    directory = _directory(directory)
    documents = _snapshot(directory)
    _validate(documents)
    _directory(destination.parent)
    _write_new(destination, _archive_documents(documents))
    return tuple(sorted(documents))


def _decode_archive(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or set(names) - (FILES.keys() | {"manifest.json"}):
                raise ValueError("unexpected or duplicate archive entries")
            if "manifest.json" not in names or len(entries) > len(FILES) + 1:
                raise ValueError("missing manifest")
            if sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES + 4096:
                raise ValueError("oversized archive")
            for entry in entries:
                if (
                    entry.flag_bits & 1
                    or entry.file_size
                    > (4096 if entry.filename == "manifest.json" else MAX_FILE_BYTES)
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise ValueError("unsupported archive member")
            manifest = _json(archive.read("manifest.json"))
            if (
                not isinstance(manifest, dict)
                or set(manifest) != {"schema_version", "build_id", "files"}
                or type(manifest["schema_version"]) is not int
                or manifest["schema_version"] != 1
                or not isinstance(manifest["build_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", manifest["build_id"])
                or not isinstance(manifest["files"], dict)
                or set(manifest["files"]) != set(names) - {"manifest.json"}
            ):
                raise ValueError("invalid manifest")
            documents = {}
            for name, record in manifest["files"].items():
                data = archive.read(name)
                if (
                    not isinstance(record, dict)
                    or set(record) != {"bytes", "sha256"}
                    or type(record["bytes"]) is not int
                    or record["bytes"] != len(data)
                    or record["sha256"] != _digest(data)
                ):
                    raise ValueError("content digest mismatch")
                documents[name] = data
        _validate(documents)
        return documents
    except ConfigurationBackupError:
        raise
    except Exception:
        raise ConfigurationBackupError(
            "This is not a valid supported Termana configuration backup."
        ) from None


def inspect_backup(path: Path) -> tuple[str, ...]:
    return prepare_restore(path).files


def prepare_restore(path: Path) -> PreparedRestore:
    raw = _read(path, MAX_ARCHIVE_BYTES)
    return PreparedRestore(tuple(sorted(_decode_archive(raw))), raw)


def prepare_settings_restore(path: Path) -> PreparedRestore:
    """Accept the raw JSON produced by automatic settings migration snapshots."""
    documents = {"settings.json": _read(path)}
    _validate(documents)
    return PreparedRestore(("settings.json",), _archive_documents(documents))


def stage_restore(directory: Path, archive: Path | PreparedRestore) -> tuple[str, ...]:
    """Queue a validated restore for next startup, before any stores are loaded."""
    directory = _directory(directory, create=True)
    if any((directory / name).exists() for name in (JOURNAL, RECOVERY_JOURNAL)):
        raise ConfigurationBackupError("An interrupted restore must be recovered on startup first.")
    raw = (
        archive.archive
        if isinstance(archive, PreparedRestore)
        else _read(archive, MAX_ARCHIVE_BYTES)
    )
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ConfigurationBackupError("Configuration backup exceeds the supported size limit.")
    documents = _decode_archive(raw)
    _write_new(directory / PENDING, raw)
    return tuple(sorted(documents))


def cancel_pending_restore(directory: Path) -> Path:
    """Retain, rather than delete, an explicitly cancelled restore request."""
    directory = _directory(directory)
    if any((directory / name).exists() for name in (JOURNAL, RECOVERY_JOURNAL)):
        raise ConfigurationBackupError("An interrupted restore must be recovered on startup first.")
    _read(directory / PENDING, MAX_ARCHIVE_BYTES)
    backups = _directory(directory / BACKUPS, create=True)
    destination = backups / f"cancelled-{uuid4().hex}.zip"
    (directory / PENDING).rename(destination)
    return destination


def _publish_journal(directory, journal):
    _read(directory / JOURNAL, missing=True)
    replace_file_atomically(directory / JOURNAL, json.dumps(journal, sort_keys=True).encode())


def _recover(directory):
    raw = _read(directory / JOURNAL, missing=True)
    if raw is None:
        return
    try:
        journal = _json(raw)
        if (
            not isinstance(journal, dict)
            or set(journal) != {"id", "files", "committed", "pending_sha256"}
            or not isinstance(journal["id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", journal["id"])
            or not isinstance(journal["pending_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", journal["pending_sha256"])
            or type(journal["committed"]) is not bool
            or not isinstance(journal["files"], dict)
            or not journal["files"]
            or set(journal["files"]) - FILES.keys()
        ):
            raise ValueError("invalid journal")
        previous = _directory(_directory(directory / BACKUPS) / journal["id"])
        if not journal["committed"]:
            originals = {}
            for name, record in journal["files"].items():
                if (
                    not isinstance(record, dict)
                    or set(record) != {"before", "after"}
                    or (
                        record["before"] is not None
                        and (
                            not isinstance(record["before"], str)
                            or not re.fullmatch(r"[0-9a-f]{64}", record["before"])
                        )
                    )
                    or not isinstance(record["after"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["after"])
                ):
                    raise ValueError("invalid restore record")
                current = _read(directory / name, missing=True)
                digest = None if current is None else _digest(current)
                if digest not in (record["before"], record["after"]):
                    raise ConfigurationBackupError(
                        "Configuration changed after an interrupted restore; recovery requires review."
                    )
                original = _read(previous / name) if record["before"] is not None else None
                if original is not None and _digest(original) != record["before"]:
                    raise ValueError("recovery backup changed")
                originals[name] = original
            for name, original in originals.items():
                if original is None:
                    (directory / name).unlink(missing_ok=True)
                else:
                    replace_file_atomically(directory / name, original)
        pending = _read(directory / PENDING, MAX_ARCHIVE_BYTES, missing=True)
        if pending is not None and _digest(pending) == journal["pending_sha256"]:
            # Keep a recoverable copy even when the queued restore was interrupted.
            request = previous / "restore-request.zip"
            retained = _read(request, MAX_ARCHIVE_BYTES, missing=True)
            if retained is None:
                (directory / PENDING).rename(request)
            elif retained == pending:
                (directory / PENDING).unlink()
            else:
                raise ValueError("recovery request changed")
        (directory / JOURNAL).unlink()
    except ConfigurationBackupError:
        raise
    except Exception:
        raise ConfigurationBackupError(
            "Interrupted configuration restore could not be recovered safely."
        ) from None


def apply_pending_restore(directory: Path) -> RestoreResult | None:
    """Call only while holding the app lock, before constructing any data stores.

    Journalled rollback covers interrupted processes; it is not a claim of
    power-loss atomicity across arbitrary filesystems. No live sessions are closed.
    """
    directory = Path(directory)
    # No explicit restore: retain the existing startup/path semantics untouched.
    if not any(
        (directory / name).exists() or (directory / name).is_symlink()
        for name in (PENDING, JOURNAL, RECOVERY_JOURNAL)
    ):
        return None
    directory = _directory(directory)
    from termana.storage.configuration_recovery import recover_interrupted_explicit_restore

    if recover_interrupted_explicit_restore(directory) is False:
        raise ConfigurationBackupError(
            "The interrupted recovery was rolled back. Review a backup before trying again."
        )
    _recover(directory)
    raw = _read(directory / PENDING, MAX_ARCHIVE_BYTES, missing=True)
    if raw is None:
        return None
    documents = _decode_archive(raw)
    previous = _directory(_directory(directory / BACKUPS, create=True) / uuid4().hex, create=True)
    records = {}
    for name, data in documents.items():
        old = _read(directory / name, missing=True)
        if old is not None:
            _write_new(previous / name, old)
        records[name] = dict(before=None if old is None else _digest(old), after=_digest(data))
    journal = dict(id=previous.name, files=records, committed=False, pending_sha256=_digest(raw))
    _publish_journal(directory, journal)
    try:
        for name, data in documents.items():
            current = _read(directory / name, missing=True)
            if (None if current is None else _digest(current)) != records[name]["before"]:
                raise ConfigurationBackupError("Configuration changed before restoration.")
            replace_file_atomically(directory / name, data)
        journal["committed"] = True
        _publish_journal(directory, journal)
    except Exception:
        _recover(directory)
        raise ConfigurationBackupError(
            "Restore did not complete; the previous configuration was recovered."
        ) from None
    _recover(directory)
    return RestoreResult(tuple(sorted(documents)), previous)
