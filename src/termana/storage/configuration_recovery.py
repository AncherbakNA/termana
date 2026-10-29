"""Explicit recovery transaction; keep conflicting originals and old restore state."""

import json
import re
from pathlib import Path
from uuid import uuid4

from termana.storage.atomic_file import replace_file_atomically
from termana.storage.configuration_backup import (
    BACKUPS,
    FILES,
    JOURNAL,
    MAX_ARCHIVE_BYTES,
    PENDING,
    RECOVERY_JOURNAL,
    ConfigurationBackupError,
    PreparedRestore,
    RestoreResult,
    _decode_archive,
    _digest,
    _directory,
    _json,
    _read,
    _write_new,
)


def _hash(data):
    return None if data is None else _digest(data)


def _sha(value, *, nullable=False):
    return (nullable and value is None) or (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
    )


def _publish(directory, journal):
    _read(directory / RECOVERY_JOURNAL, missing=True)
    replace_file_atomically(
        directory / RECOVERY_JOURNAL, json.dumps(journal, sort_keys=True).encode()
    )


def recover_interrupted_explicit_restore(directory: Path) -> bool | None:
    """True=committed cleanup; False=rolled back; None=no recovery transaction.

    A conflicting external edit is never overwritten by automatic recovery.
    Retained source copies are not deleted. Not a power-loss atomicity guarantee.
    """
    raw = _read(directory / RECOVERY_JOURNAL, missing=True)
    if raw is None:
        return None
    try:
        journal = _json(raw)
        if (
            not isinstance(journal, dict)
            or set(journal) != {"schema_version", "id", "committed", "files", "markers"}
            or type(journal["schema_version"]) is not int
            or journal["schema_version"] != 1
            or not isinstance(journal["id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", journal["id"])
            or type(journal["committed"]) is not bool
            or not isinstance(journal["files"], dict)
            or not journal["files"]
            or set(journal["files"]) - FILES.keys()
            or not isinstance(journal["markers"], dict)
            or set(journal["markers"]) != {JOURNAL, PENDING}
        ):
            raise ValueError("invalid recovery journal")
        previous = _directory(_directory(directory / BACKUPS) / journal["id"])
        documents = _decode_archive(_read(previous / "selected-backup.zip", MAX_ARCHIVE_BYTES))
        if set(documents) != set(journal["files"]):
            raise ValueError("recovery files changed")
        originals = {}
        for name, record in journal["files"].items():
            if (
                not isinstance(record, dict)
                or set(record) != {"before", "after"}
                or not _sha(record["before"], nullable=True)
                or not _sha(record["after"])
                or _digest(documents[name]) != record["after"]
            ):
                raise ValueError("invalid recovery record")
            current = _hash(_read(directory / name, missing=True))
            expected = (
                (record["after"],) if journal["committed"] else (record["before"], record["after"])
            )
            if current not in expected:
                raise ConfigurationBackupError(
                    "Configuration changed during recovery; originals are retained for review."
                )
            original = _read(previous / name) if record["before"] is not None else None
            if _hash(original) != record["before"]:
                raise ValueError("recovery originals changed")
            originals[name] = original
        # Validate every marker/copy before mutating any file. Partially cleaned
        # committed transactions may already have removed one active marker.
        for name, digest in journal["markers"].items():
            if not _sha(digest, nullable=True):
                raise ValueError("invalid marker record")
            current = _hash(_read(directory / name, MAX_ARCHIVE_BYTES, missing=True))
            allowed = (None, digest) if journal["committed"] else (digest,)
            if current not in allowed:
                raise ConfigurationBackupError(
                    "Restore state changed during recovery; retained files need review."
                )
            archived = _read(previous / name, MAX_ARCHIVE_BYTES) if digest is not None else None
            if _hash(archived) != digest:
                raise ValueError("retained marker changed")
        if journal["committed"]:
            for name in journal["markers"]:
                (directory / name).unlink(missing_ok=True)
        else:
            for name, original in originals.items():
                if original is None:
                    (directory / name).unlink(missing_ok=True)
                else:
                    replace_file_atomically(directory / name, original)
        (directory / RECOVERY_JOURNAL).unlink()
        return journal["committed"]
    except ConfigurationBackupError:
        raise
    except Exception:
        raise ConfigurationBackupError(
            "The interrupted recovery could not be completed safely; keep all retained files."
        ) from None


def restore_for_recovery(directory: Path, prepared: PreparedRestore) -> RestoreResult:
    """Explicit confirmation is required: replace selected files, not live stores."""
    directory = _directory(directory)
    if (directory / RECOVERY_JOURNAL).exists() or (directory / RECOVERY_JOURNAL).is_symlink():
        raise ConfigurationBackupError(
            "A previous recovery transaction requires review before another restore."
        )
    if len(prepared.archive) > MAX_ARCHIVE_BYTES:
        raise ConfigurationBackupError("Backup exceeds the supported size limit.")
    documents = _decode_archive(prepared.archive)
    previous = _directory(_directory(directory / BACKUPS, create=True) / uuid4().hex, create=True)
    records, markers = {}, {}
    for name, data in documents.items():
        original = _read(directory / name, missing=True)
        if original is not None:
            _write_new(previous / name, original)
        records[name] = dict(before=_hash(original), after=_digest(data))
    for name in (JOURNAL, PENDING):
        original = _read(directory / name, MAX_ARCHIVE_BYTES, missing=True)
        if original is not None:
            _write_new(previous / name, original)
        markers[name] = _hash(original)
    _write_new(previous / "selected-backup.zip", prepared.archive)
    journal = dict(
        schema_version=1, id=previous.name, files=records, markers=markers, committed=False
    )
    _publish(directory, journal)
    try:
        for name, data in documents.items():
            if _hash(_read(directory / name, missing=True)) != records[name]["before"]:
                raise ConfigurationBackupError("Configuration changed before recovery.")
            replace_file_atomically(directory / name, data)
        journal["committed"] = True
        _publish(directory, journal)
    except Exception:
        recover_interrupted_explicit_restore(directory)
        raise ConfigurationBackupError(
            "Recovery did not finish; previous files were retained and recovered."
        ) from None
    recover_interrupted_explicit_restore(directory)
    return RestoreResult(tuple(sorted(documents)), previous)
